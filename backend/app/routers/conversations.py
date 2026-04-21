import asyncio
import json
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.conversation import Conversation, Message, MessageRole
from app.models.knowledge import KnowledgeEntry
from app.models.user import User
from app.services.skill_engine import skill_engine
from app.services.llm_gateway import llm_gateway
from app.utils.file_parser import extract_text

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# ── SSE Keepalive ─────────────────────────────────────────────────────────────
_SSE_KEEPALIVE = ": ping\n\n"  # SSE comment — ignored by browser but keeps proxy alive
_KEEPALIVE_INTERVAL = 15  # seconds of silence before emitting a ping
_SSE_TOTAL_TIMEOUT = 600  # H6: SSE 连接总超时 (秒)

import time as _time


async def _stream_with_keepalive(agen, request=None):
    """Wrap an async generator: yield items as they arrive;
    if no item arrives within _KEEPALIVE_INTERVAL seconds, yield a keepalive ping.
    Prevents nginx / Next.js proxy from closing idle SSE connections during
    long LLM thinking phases.

    H6: 添加总超时 + 客户端断连检测。
    """
    deadline = _time.monotonic() + _SSE_TOTAL_TIMEOUT
    it = agen.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            # H6: 总超时检查
            if _time.monotonic() > deadline:
                logger.warning("SSE 连接总超时 (%ds)，强制关闭", _SSE_TOTAL_TIMEOUT)
                break
            # H6: 客户端断连检测
            if request and await request.is_disconnected():
                logger.info("SSE 客户端已断开连接")
                break
            try:
                item = await asyncio.wait_for(asyncio.shield(pending), timeout=_KEEPALIVE_INTERVAL)
                pending = asyncio.ensure_future(it.__anext__())
                yield item
            except asyncio.TimeoutError:
                yield _SSE_KEEPALIVE  # keepalive ping, loop continues
            except StopAsyncIteration:
                break
    finally:
        # M10: 确保 future 和底层 generator 都正确清理
        pending.cancel()
        try:
            await pending
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        # 关闭底层 async generator，释放资源
        try:
            await it.aclose()
        except Exception:
            pass


def _classify_error(e: Exception) -> str:
    """Classify exception into error type for frontend."""
    msg = str(e).lower()
    if "rate" in msg or "429" in msg or "quota" in msg:
        return "rate_limit"
    if "context" in msg or "token" in msg or "length" in msg or "too long" in msg:
        return "context_overflow"
    if "connect" in msg or "timeout" in msg or "network" in msg:
        return "network"
    return "server_error"


class SendMessage(BaseModel):
    content: str
    active_skill_ids: list[int] | None = None
    force_skill_id: int | None = None  # 沙盒测试模式：强制指定 skill（允许 draft，仅限本人创建）
    # Studio Agent 编辑上下文（仅 skill_studio workspace 使用）
    selected_skill_id: int | None = None
    editor_prompt: str | None = None
    editor_is_dirty: bool = False
    selected_source_filename: str | None = None
    active_card_id: str | None = None
    active_card_title: str | None = None
    active_card_mode: str | None = None
    active_card_target: str | None = None
    active_card_source_card_id: str | None = None
    active_card_staged_edit_id: str | None = None
    active_card_validation_source: dict | None = None

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("消息内容不能为空")
        # L3: 消息内容最大长度 8K
        if len(v) > 8192:
            raise ValueError("消息内容超过最大长度限制 (8192 字符)")
        return v


@router.get("/studio-entry")
def studio_entry(
    type: str = "skill_studio",
    skill_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """统一 Studio 入口 API — 返回注册表 + conversation + runtime 状态。"""
    from app.services.studio_registry import resolve_entry
    try:
        entry = resolve_entry(db, user, type, skill_id=skill_id)
    except Exception as e:
        logger.exception(f"studio_entry failed: user={user.id}, type={type}, skill_id={skill_id}")
        raise HTTPException(500, f"Studio 入口初始化失败：{e.__class__.__name__}: {e}")
    return {
        "registration_id": entry.registration_id,
        "conversation_id": entry.conversation_id,
        "workspace_root": entry.workspace_root,
        "project_dir": entry.project_dir,
        "runtime_status": entry.runtime_status,
        "runtime_port": entry.runtime_port,
        "generation": entry.generation,
        "needs_recover": entry.needs_recover,
    }


@router.post("/studio-entry/migrate-skill-conversations")
def migrate_skill_conversations_api(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将用户旧共享 Skill Studio 会话中的消息按 skill_id 迁移到独立 conversation。"""
    from app.services.studio_registry import migrate_skill_conversations
    result = migrate_skill_conversations(db, user)
    return result


class StudioSessionRouteRequest(BaseModel):
    skill_id: Optional[int] = None


@router.post("/studio-sessions/{conv_id}/route")
def studio_session_route(
    conv_id: int,
    req: StudioSessionRouteRequest = StudioSessionRouteRequest(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Studio session 路由 — 根据 skill 属性返回会话模式。"""
    from app.services.studio_router import route_session

    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    skill_id = req.skill_id or conv.skill_id
    # 取最近一条 user message 作为 intent 判断素材
    _latest_user_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conv_id, Message.role == MessageRole.USER)
        .order_by(Message.created_at.desc())
        .first()
    )
    _user_text = _latest_user_msg.content if _latest_user_msg else ""
    result = route_session(db, skill_id=skill_id, user_message=_user_text)

    resp = {
        "session_mode": result.session_mode,
        "active_assist_skills": result.active_assist_skills,
        "route_reason": result.route_reason,
        "next_action": result.next_action,
        "workflow_mode": result.workflow_mode,
        "initial_phase": result.initial_phase,
    }

    # 如果启用了 architect_mode，创建/更新持久化状态
    if result.workflow_mode == "architect_mode":
        from app.models.skill import ArchitectWorkflowState
        existing = db.query(ArchitectWorkflowState).filter(
            ArchitectWorkflowState.conversation_id == conv_id
        ).first()
        if not existing:
            state = ArchitectWorkflowState(
                conversation_id=conv_id,
                skill_id=skill_id,
                workflow_mode="architect_mode",
                workflow_phase=result.initial_phase,
            )
            db.add(state)
            db.commit()

    return resp


# ── Architect 状态读写 ───────────────────────────────────────────────────────


class ArchitectStateUpdate(BaseModel):
    workflow_phase: Optional[str] = None
    phase_outputs: Optional[dict] = None
    ooda_round: Optional[int] = None
    phase_confirmed: Optional[dict] = None


@router.get("/conversations/{conv_id}/architect-state")
def get_architect_state(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取 architect 工作流阶段状态。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    from app.models.skill import ArchitectWorkflowState
    state = db.query(ArchitectWorkflowState).filter(
        ArchitectWorkflowState.conversation_id == conv_id
    ).first()
    if not state:
        return {"workflow_mode": "none"}

    return {
        "workflow_mode": state.workflow_mode,
        "workflow_phase": state.workflow_phase,
        "phase_outputs": state.phase_outputs or {},
        "ooda_round": state.ooda_round,
        "phase_confirmed": state.phase_confirmed or {},
        "skill_id": state.skill_id,
    }


@router.patch("/conversations/{conv_id}/architect-state")
def update_architect_state(
    conv_id: int,
    req: ArchitectStateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新 architect 工作流阶段状态（阶段推进 / 确认 / OODA 轮次）。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    from app.models.skill import ArchitectWorkflowState
    state = db.query(ArchitectWorkflowState).filter(
        ArchitectWorkflowState.conversation_id == conv_id
    ).first()
    if not state:
        raise HTTPException(404, "No architect state for this conversation")

    if req.workflow_phase is not None:
        state.workflow_phase = req.workflow_phase
    if req.phase_outputs is not None:
        # merge into existing
        existing = state.phase_outputs or {}
        existing.update(req.phase_outputs)
        state.phase_outputs = existing
    if req.ooda_round is not None:
        state.ooda_round = req.ooda_round
    if req.phase_confirmed is not None:
        existing = state.phase_confirmed or {}
        existing.update(req.phase_confirmed)
        state.phase_confirmed = existing

    db.commit()
    db.refresh(state)

    return {
        "ok": True,
        "workflow_mode": state.workflow_mode,
        "workflow_phase": state.workflow_phase,
        "phase_outputs": state.phase_outputs or {},
        "ooda_round": state.ooda_round,
        "phase_confirmed": state.phase_confirmed or {},
    }


class ConversationCreate(BaseModel):
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None


@router.get("/{conv_id}/studio-runs/active")
async def get_active_studio_run(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    from app.services.studio_runs import studio_run_registry

    run = await studio_run_registry.get_active(conv_id, user.id)
    return {"run": run.summary() if run else None}


@router.get("/{conv_id}/studio-runs/{run_id}/events")
async def stream_studio_run_events(
    conv_id: int,
    run_id: str,
    after: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    from app.services.studio_runs import studio_run_registry

    run = await studio_run_registry.get(run_id, user.id)
    if not run or run.conversation_id != conv_id:
        raise HTTPException(404, "Studio run not found")
    return StreamingResponse(
        studio_run_registry.stream(run, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{conv_id}/studio-runs/{run_id}/cancel")
async def cancel_studio_run(
    conv_id: int,
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    from app.services.studio_runs import studio_run_registry

    run = await studio_run_registry.cancel(run_id, user.id)
    if not run or run.conversation_id != conv_id:
        raise HTTPException(404, "Studio run not found")
    return {"run": run.summary()}


@router.get("/{conv_id}/studio-state")
def get_studio_state(
    conv_id: int,
    skill_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    from app.harness.contracts import AgentType, HarnessSessionKey
    from app.harness.gateway import get_session_store

    resolved_skill_id = skill_id or conv.skill_id
    if not resolved_skill_id:
        return {"studio_state": None}

    session_key = HarnessSessionKey(
        user_id=user.id,
        agent_type=AgentType.SKILL_STUDIO,
        workspace_id=conv.workspace_id or 0,
        target_type="skill",
        target_id=resolved_skill_id,
    )
    store = get_session_store()
    snapshot = store.get_studio_state_snapshot(session_key, db=db)
    return {
        "studio_state": snapshot["studio_state"],
        "recovery": snapshot["recovery"],
        "skill_id": resolved_skill_id,
        "conversation_id": conv_id,
    }


@router.post("")
def create_conversation(
    req: ConversationCreate = ConversationCreate(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # opencode / sandbox workspace：每人只能有一个对话，若已存在则直接返回
    if req.workspace_id:
        from app.models.workspace import Workspace as WsModel
        ws = db.get(WsModel, req.workspace_id)
        if ws and ws.workspace_type == "sandbox":
            existing = (
                db.query(Conversation)
                .filter(
                    Conversation.user_id == user.id,
                    Conversation.workspace_id == req.workspace_id,
                    Conversation.is_active == True,
                )
                .first()
            )
            if existing:
                return {"id": existing.id, "title": existing.title, "workspace_id": existing.workspace_id}
            conv = Conversation(user_id=user.id, workspace_id=req.workspace_id, title="沙盒测试")
            db.add(conv)
            db.commit()
            db.refresh(conv)
            return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id}
        if ws and ws.workspace_type == "opencode":
            existing = (
                db.query(Conversation)
                .filter(
                    Conversation.user_id == user.id,
                    Conversation.workspace_id == req.workspace_id,
                    Conversation.is_active == True,
                )
                .first()
            )
            if existing:
                return {"id": existing.id, "title": existing.title, "workspace_id": existing.workspace_id}
            # 首次创建，标题固定
            conv = Conversation(user_id=user.id, workspace_id=req.workspace_id, title="OpenCode 开发")
            db.add(conv)
            db.commit()
            db.refresh(conv)
            return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id}
        if ws and ws.workspace_type == "skill_studio":
            existing = (
                db.query(Conversation)
                .filter(
                    Conversation.user_id == user.id,
                    Conversation.workspace_id == req.workspace_id,
                    Conversation.is_active == True,
                )
                .first()
            )
            if existing:
                return {"id": existing.id, "title": existing.title, "workspace_id": existing.workspace_id}
            conv = Conversation(user_id=user.id, workspace_id=req.workspace_id, title=ws.name)
            db.add(conv)
            db.commit()
            db.refresh(conv)
            return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id}

    # project 对话：每人只维护一个（类似 opencode 逻辑）
    if req.project_id and not req.workspace_id:
        existing = (
            db.query(Conversation)
            .filter(
                Conversation.user_id == user.id,
                Conversation.project_id == req.project_id,
                Conversation.is_active == True,
            )
            .first()
        )
        if existing:
            return {"id": existing.id, "title": existing.title, "workspace_id": existing.workspace_id, "project_id": existing.project_id}
        conv = Conversation(user_id=user.id, project_id=req.project_id, title="项目对话")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id, "project_id": conv.project_id}

    conv = Conversation(user_id=user.id, workspace_id=req.workspace_id, project_id=req.project_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id, "project_id": conv.project_id}


@router.get("")
def list_conversations(
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Conversation).filter(Conversation.is_active == True)
    if project_id is not None:
        # 项目群组视图：返回该项目所有成员的对话
        from app.models.project import ProjectMember
        member_ids = [
            row[0] for row in
            db.query(ProjectMember.user_id).filter(ProjectMember.project_id == project_id).all()
        ]
        q = q.filter(
            Conversation.project_id == project_id,
            Conversation.user_id.in_(member_ids),
        )
    else:
        q = q.filter(Conversation.user_id == user.id)
    convs = q.order_by(Conversation.updated_at.desc()).limit(50).all()
    def _conv_dict(c: Conversation) -> dict:
        from app.models.workspace import Workspace
        ws = db.get(Workspace, c.workspace_id) if c.workspace_id else None
        owner = db.get(User, c.user_id) if c.user_id else None
        last_msg = c.messages[-1] if c.messages else None
        return {
            "id": c.id,
            "title": c.title,
            "skill_id": c.skill_id,
            "workspace_id": c.workspace_id,
            "project_id": c.project_id,
            "workspace": {"name": ws.name, "icon": ws.icon, "color": ws.color} if ws else None,
            "workspace_type": ws.workspace_type if ws else None,
            "updated_at": c.updated_at.isoformat(),
            "owner_id": c.user_id,
            "owner_name": owner.display_name if owner else None,
            "last_message": last_msg.content[:100] if last_msg else None,
        }

    return [_conv_dict(c) for c in convs]


@router.get("/{conv_id}/messages")
def get_messages(
    conv_id: int,
    skill_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    # 项目对话：项目成员可互相查看消息
    if conv.project_id:
        from app.models.project import ProjectMember
        is_member = db.query(ProjectMember).filter(
            ProjectMember.project_id == conv.project_id,
            ProjectMember.user_id == user.id,
        ).first()
        if not is_member and conv.user_id != user.id:
            raise HTTPException(403, "无权访问该对话")
    elif conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    messages = conv.messages
    # 按 skill_id 过滤（Skill Studio 视图模式）
    if skill_id is not None:
        messages = [
            m for m in messages
            if m.metadata_ and m.metadata_.get("skill_id") == skill_id
        ]

    owner = db.get(User, conv.user_id) if conv.user_id else None
    return [
        {
            "id": m.id,
            "role": m.role.value,
            "content": m.content,
            "metadata": m.metadata_,
            "created_at": m.created_at.isoformat(),
            "sender_id": conv.user_id,
            "sender_name": owner.display_name if owner else None,
        }
        for m in messages
    ]


@router.delete("/{conv_id}/messages")
def clear_messages(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """清空对话的所有消息（仅对话所有者可操作）。"""
    conv = db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if conv.user_id != user.id:
        raise HTTPException(403, "无权操作该对话")
    deleted = db.query(Message).filter(Message.conversation_id == conv_id).delete()
    db.commit()
    return {"ok": True, "deleted": deleted}


@router.post("/{conv_id}/messages")
async def send_message(
    conv_id: int,
    req: SendMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # opencode 工作台对话标题锁定，不随消息内容变更
    _is_opencode_conv = False
    _ws_obj = None
    if conv.workspace_id:
        from app.models.workspace import Workspace as WsModel
        _ws_obj = db.get(WsModel, conv.workspace_id)
        if _ws_obj and _ws_obj.workspace_type == "sandbox":
            # 沙盒工作台：必须携带 force_skill_id，拒绝一切无关请求
            if not req.force_skill_id:
                raise HTTPException(400, "沙盒测试工作台仅用于测试指定 Skill，请从技能列表发起测试")
        if _ws_obj and _ws_obj.workspace_type == "opencode":
            _is_opencode_conv = True

    # Persist user message immediately so it survives any downstream failure
    _user_msg_meta: dict = {}
    if req.selected_skill_id is not None:
        _user_msg_meta["skill_id"] = req.selected_skill_id
    if _ws_obj and _ws_obj.workspace_type == "skill_studio":
        _user_msg_meta["studio_scope"] = "skill_studio"
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=req.content,
        metadata_=_user_msg_meta if _user_msg_meta else {},
    )
    db.add(user_msg)
    db.commit()

    # G3: skill_studio 同步路径统一走 SkillStudioAgentProfile（消除 system_context 直聊双轨）
    if _ws_obj and _ws_obj.workspace_type == "skill_studio":
        try:
            from app.harness.profiles.skill_studio import skill_studio_profile
            from app.harness.adapters import build_skill_studio_request
            _studio_req = build_skill_studio_request(
                user_id=user.id,
                workspace_id=conv.workspace_id or 0,
                skill_id=req.selected_skill_id or conv.skill_id or 0,
                conversation_id=conv_id,
                user_message=req.content,
                stream=False,
                metadata={"source": "conversations.send_message"},
            )
            _studio_resp = await skill_studio_profile.run_sync(
                _studio_req, db, conv,
                selected_skill_id=req.selected_skill_id,
                editor_prompt=req.editor_prompt,
                editor_is_dirty=req.editor_is_dirty,
            )
            if _studio_resp.error:
                raise HTTPException(503, _studio_resp.error)
            _resp_text = _studio_resp.content
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"SkillStudioAgentProfile sync error: {e}")
            raise HTTPException(503, f"AI 服务暂时不可用，请稍后重试")
        _assistant_msg = Message(
            conversation_id=conv_id,
            role=MessageRole.ASSISTANT,
            content=_resp_text,
            metadata_={"skill_id": req.selected_skill_id, "studio_scope": "skill_studio"},
        )
        db.add(_assistant_msg)
        msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
        if msg_count <= 2:
            conv.title = req.content[:60]
        db.commit()
        return {
            "id": _assistant_msg.id,
            "role": "assistant",
            "content": _resp_text,
            "skill_id": req.selected_skill_id,
            "skill_name": None,
            "metadata": {"studio_scope": "skill_studio"},
        }

    # 同步入口保持 legacy execute 兼容；统一 runtime 继续用于流式主链。
    try:
        response, guide_meta = await skill_engine.execute(
            db,
            conv,
            req.content,
            user_id=user.id,
            active_skill_ids=req.active_skill_ids,
            force_skill_id=req.force_skill_id,
        )
    except ValueError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error(f"SkillEngine sync error: {e}")
        raise HTTPException(503, f"AI 服务暂时不可用，请稍后重试")

    # Resolve skill name
    skill_name = None
    if conv.skill_id:
        from app.models.skill import Skill as SkillModel
        sk = db.get(SkillModel, conv.skill_id)
        skill_name = sk.name if sk else None

    # Extract token usage from guide_meta
    llm_usage = guide_meta.pop("llm_usage", {})
    msg_metadata = {
        "skill_id": req.selected_skill_id or conv.skill_id,
        "skill_name": skill_name,
        "model_id": llm_usage.get("model_id"),
        "input_tokens": llm_usage.get("input_tokens", 0),
        "output_tokens": llm_usage.get("output_tokens", 0),
        **guide_meta,
    }
    if _ws_obj and _ws_obj.workspace_type == "skill_studio":
        msg_metadata["studio_scope"] = "skill_studio"

    # Persist assistant response
    assistant_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        content=response,
        metadata_=msg_metadata,
    )
    db.add(assistant_msg)

    # Update conversation title on first exchange
    msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
    if msg_count <= 2:
        if not _is_opencode_conv:
            conv.title = req.content[:60]

    db.commit()
    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": response,
        "skill_id": conv.skill_id,
        "skill_name": skill_name,
        "metadata": msg_metadata,
    }


@router.post("/{conv_id}/messages/stream")
async def stream_message(
    conv_id: int,
    req: SendMessage,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming endpoint: same logic as send_message but streams LLM output."""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # G1: 标准化入口请求（旁路接线，不改变现有执行链）
    try:
        from app.harness.adapters import build_chat_request, build_skill_studio_request
        from app.models.workspace import Workspace as _HarnessWsModel
        _ws_for_harness = db.get(_HarnessWsModel, conv.workspace_id) if conv.workspace_id else None
        if _ws_for_harness and _ws_for_harness.workspace_type == "skill_studio":
            _h_req = build_skill_studio_request(
                user_id=user.id,
                workspace_id=conv.workspace_id or 0,
                skill_id=req.selected_skill_id or conv.skill_id or 0,
                conversation_id=conv_id,
                user_message=req.content,
                stream=True,
                metadata={"source": "conversations.stream"},
            ) if (req.selected_skill_id or conv.skill_id) else None
        else:
            _h_req = build_chat_request(
                user_id=user.id,
                workspace_id=conv.workspace_id or 0,
                conversation_id=conv_id,
                user_message=req.content,
                stream=True,
                metadata={"source": "conversations.stream"},
            ) if conv.workspace_id else None
    except Exception:
        _h_req = None

    # 沙盒工作台 guard：在 commit user message 之前检查，避免无关消息入库
    if conv.workspace_id:
        from app.models.workspace import Workspace as WsModel
        _ws_pre = db.get(WsModel, conv.workspace_id)
        if _ws_pre and _ws_pre.workspace_type == "sandbox" and not req.force_skill_id:
            raise HTTPException(400, "沙盒测试工作台仅用于测试指定 Skill，请从技能列表发起测试")

    # 判断 workspace_type 以便补齐 studio_scope
    _ws_type_stream = None
    if conv.workspace_id:
        from app.models.workspace import Workspace as _WsTypeModel
        _ws_t = db.get(_WsTypeModel, conv.workspace_id)
        if _ws_t:
            _ws_type_stream = _ws_t.workspace_type

    # Persist user message — commit immediately so it survives if SSE is dropped mid-stream
    _user_meta: dict = {}
    if req.selected_skill_id:
        _user_meta["skill_id"] = req.selected_skill_id
    if req.active_card_id:
        _user_meta["active_card_id"] = req.active_card_id
    if req.active_card_source_card_id:
        _user_meta["active_card_source_card_id"] = req.active_card_source_card_id
    if req.active_card_staged_edit_id:
        _user_meta["active_card_staged_edit_id"] = req.active_card_staged_edit_id
    if _ws_type_stream == "skill_studio":
        _user_meta["studio_scope"] = "skill_studio"
        if req.active_card_title:
            _user_meta["active_card_title"] = req.active_card_title
        if req.active_card_mode:
            _user_meta["active_card_mode"] = req.active_card_mode
        if req.active_card_target:
            _user_meta["active_card_target"] = req.active_card_target
        if req.active_card_validation_source:
            _user_meta["active_card_validation_source"] = req.active_card_validation_source
    if req.editor_prompt:
        _user_meta["editor_target"] = True
    if _h_req:
        _user_meta["_harness_request_id"] = _h_req.request_id
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=req.content,
        metadata_=_user_meta,
    )
    db.add(user_msg)
    db.commit()

    # Capture user.id as a plain int before the generator closes over it —
    # the ORM User object becomes detached once the session flushes/expires.
    current_user_id = user.id

    if _ws_type_stream == "skill_studio":
        from app.services.studio_runs import studio_run_registry

        run = await studio_run_registry.create(
            conversation_id=conv_id,
            user_id=current_user_id,
            skill_id=req.selected_skill_id or conv.skill_id,
            content=req.content,
            req_payload={
                "editor_prompt": req.editor_prompt,
                "editor_is_dirty": req.editor_is_dirty,
                "selected_source_filename": req.selected_source_filename,
                "active_card_id": req.active_card_id,
                "active_card_title": req.active_card_title,
                "active_card_mode": req.active_card_mode,
                "active_card_target": req.active_card_target,
                "active_card_source_card_id": req.active_card_source_card_id,
                "active_card_staged_edit_id": req.active_card_staged_edit_id,
                "active_card_validation_source": req.active_card_validation_source,
            },
        )
        return StreamingResponse(
            studio_run_registry.stream(run, after=0),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Studio-Run-Id": run.id,
            },
        )

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # C4 fix: 将外层 db 保存，generator 中创建独立 Session
    _outer_db = db

    async def event_generator():
        import time as _time_mod
        _stream_start = _time_mod.monotonic()
        _tool_call_count = 0
        _tool_error_count = 0
        _stream_success = True
        _stream_error_type: str | None = None

        from app.database import SessionLocal
        # C4: 优先复用外层 Session（测试环境仍有效），否则创建独立 Session
        _owns_session = False
        try:
            _outer_db.execute(text("SELECT 1"))
            db = _outer_db
        except Exception:
            db = SessionLocal()
            _owns_session = True
        try:
            # Re-fetch conv inside generator to avoid detached instance issues after outer flush
            conv = db.get(Conversation, conv_id)
            if not conv:
                yield _sse("error", {"message": "Conversation not found", "error_type": "server_error", "retryable": False})
                return

            # opencode 工作台对话标题锁定，不随消息内容变更
            _is_opencode_conv = False
            if conv.workspace_id:
                from app.models.workspace import Workspace as WsModel
                _ws = db.get(WsModel, conv.workspace_id)
                if _ws and _ws.workspace_type == "opencode":
                    _is_opencode_conv = True

            # --- PEV 升级判断（复杂多步场景） ---
            # skill_studio workspace 是交互式对话，不走 PEV
            _skip_pev = False
            if conv.workspace_id:
                from app.models.workspace import Workspace as WsModel2
                _ws_pev = db.get(WsModel2, conv.workspace_id)
                if _ws_pev and _ws_pev.workspace_type in ("skill_studio", "sandbox", "opencode"):
                    _skip_pev = True

            from app.models.skill import Skill as SkillModel
            from app.services.pev import pev_orchestrator
            from app.models.pev_job import PEVJob

            _skill_for_pev = db.get(SkillModel, conv.skill_id) if conv.skill_id else None
            pev_scenario = None if _skip_pev else await pev_orchestrator.should_upgrade(req.content, _skill_for_pev, conv, db)
            if pev_scenario:
                pev_job = PEVJob(
                    scenario=pev_scenario,
                    goal=req.content,
                    conversation_id=conv_id,
                    user_id=current_user_id,
                    config={},
                )
                db.add(pev_job)
                db.commit()
                db.refresh(pev_job)

                pev_summary = ""
                async for event in pev_orchestrator.run(db, pev_job):
                    yield _sse(event["event"], event["data"])
                    if event["event"] == "pev_done":
                        pev_summary = event["data"].get("summary", "")

                # 持久化 PEV 结果为 assistant 消息，并发 done 事件让前端关闭 isSending
                pev_msg = Message(
                    conversation_id=conv_id,
                    role=MessageRole.ASSISTANT,
                    content=pev_summary or f"任务已完成（{pev_job.scenario}）",
                    metadata_={"pev_job_id": pev_job.id, "skill_id": conv.skill_id},
                )
                db.add(pev_msg)
                msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
                if msg_count <= 2 and not _is_opencode_conv:
                    conv.title = req.content[:60]
                db.commit()
                yield _sse("done", {"message_id": pev_msg.id, "metadata": {"pev_job_id": pev_job.id}})
                return

            # --- G3: skill_studio 流式路径统一走 SkillStudioAgentProfile ---
            if _skip_pev and conv.workspace_id:
                from app.models.workspace import Workspace as WsModel3
                _ws_fast = db.get(WsModel3, conv.workspace_id)
                if _ws_fast and _ws_fast.workspace_type == "skill_studio":
                    from app.harness.profiles.skill_studio import skill_studio_profile
                    from app.harness.adapters import build_skill_studio_request as _build_studio_req
                    logger.info(
                        f"[studio_profile] conv={conv_id} skill={req.selected_skill_id} "
                        f"dirty={req.editor_is_dirty}"
                    )

                    _studio_req = _build_studio_req(
                        user_id=current_user_id,
                        workspace_id=conv.workspace_id or 0,
                        skill_id=req.selected_skill_id or conv.skill_id or 0,
                        conversation_id=conv_id,
                        user_message=req.content,
                        stream=True,
                        metadata={"source": "conversations.stream"},
                    )

                    # ── Studio 结构化模式（灰度开关）：首轮 route + audit ──
                    from app.config import settings as _app_settings
                    _studio_structured = _app_settings.STUDIO_STRUCTURED_MODE == "on"

                    if _studio_structured:
                        _msg_count_check = db.query(Message).filter(
                            Message.conversation_id == conv_id
                        ).count()
                        if _msg_count_check <= 2:
                            from app.services.studio_router import route_session
                            from app.services.studio_latency_policy import (
                                choose_execution_strategy,
                                estimate_complexity_level,
                            )
                            from app.services.studio_rollout import (
                                apply_rollout_to_execution_strategy,
                                lane_statuses_for_rollout,
                                resolve_rollout_decision,
                            )
                            _route_result = route_session(
                                db, skill_id=req.selected_skill_id, user_message=req.content,
                            )
                            _complexity_level = estimate_complexity_level(
                                session_mode=_route_result.session_mode,
                                workflow_mode=_route_result.workflow_mode,
                                next_action=_route_result.next_action,
                                user_message=req.content,
                                has_files=False,
                                has_memo=bool(req.selected_skill_id),
                                history_count=_msg_count_check,
                            )
                            _execution_strategy = choose_execution_strategy(
                                complexity_level=_complexity_level,
                                workflow_mode=_route_result.workflow_mode,
                                next_action=_route_result.next_action,
                            )
                            _rollout_decision = resolve_rollout_decision(
                                db,
                                user_id=current_user_id,
                                session_mode=_route_result.session_mode,
                                workflow_mode=_route_result.workflow_mode,
                            )
                            _execution_strategy = apply_rollout_to_execution_strategy(
                                _execution_strategy,
                                flags=_rollout_decision.flags,
                            )
                            _lane_statuses = lane_statuses_for_rollout(_execution_strategy, flags=_rollout_decision.flags)
                            yield _sse("route_status", {
                                "session_mode": _route_result.session_mode,
                                "active_assist_skills": _route_result.active_assist_skills,
                                "route_reason": _route_result.route_reason,
                                "next_action": _route_result.next_action,
                                "workflow_mode": _route_result.workflow_mode,
                                "initial_phase": _route_result.initial_phase,
                                "complexity_level": _complexity_level,
                                "execution_strategy": _execution_strategy,
                                **_lane_statuses,
                            })
                            yield _sse("assist_skills_status", {
                                "skills": _route_result.active_assist_skills,
                                "session_mode": _route_result.session_mode,
                            })

                            if _route_result.workflow_mode == "architect_mode":
                                from app.models.skill import ArchitectWorkflowState
                                _arch_state = db.query(ArchitectWorkflowState).filter(
                                    ArchitectWorkflowState.conversation_id == conv_id
                                ).first()
                                if not _arch_state:
                                    _arch_state = ArchitectWorkflowState(
                                        conversation_id=conv_id,
                                        skill_id=req.selected_skill_id,
                                        workflow_mode="architect_mode",
                                        workflow_phase=_route_result.initial_phase,
                                    )
                                    db.add(_arch_state)
                                    db.commit()
                                    db.refresh(_arch_state)
                                yield _sse("architect_phase_status", {
                                    "phase": _arch_state.workflow_phase,
                                    "mode_source": _route_result.session_mode,
                                    "ooda_round": _arch_state.ooda_round,
                                })

                            if _route_result.next_action == "run_audit" and req.selected_skill_id:
                                try:
                                    from app.services.studio_auditor import run_audit as _run_audit
                                    _audit_result = await _run_audit(db, req.selected_skill_id)
                                    yield _sse("audit_summary", {
                                        "verdict": _audit_result.verdict,
                                        "issues": _audit_result.issues,
                                        "recommended_path": _audit_result.recommended_path,
                                        "audit_id": getattr(_audit_result, "audit_id", None),
                                    })
                                    if _audit_result.verdict in ("needs_work", "poor"):
                                        try:
                                            from app.services.studio_governance import generate_governance_actions
                                            _gov_result = await generate_governance_actions(
                                                db, req.selected_skill_id,
                                                audit_id=getattr(_audit_result, "audit_id", None),
                                            )
                                            for _card in _gov_result.cards:
                                                yield _sse("governance_card", _card)
                                            for _se in _gov_result.staged_edits:
                                                yield _sse("staged_edit_notice", _se)
                                        except Exception as _gov_err:
                                            logger.warning(f"[studio] auto-governance failed: {_gov_err}")
                                            yield _sse("fallback_text", {"text": f"治理建议生成失败: {_gov_err}"})
                                except Exception as _audit_err:
                                    logger.warning(f"[studio] auto-audit failed: {_audit_err}")
                                    yield _sse("fallback_text", {"text": f"审计未能完成: {_audit_err}"})

                    # G3: 通过 SkillStudioAgentProfile 执行（唯一主链）
                    _final_content = ""
                    async for _harness_evt in skill_studio_profile.run_stream(
                        _studio_req, db, conv,
                        selected_skill_id=req.selected_skill_id,
                        editor_prompt=req.editor_prompt,
                        editor_is_dirty=req.editor_is_dirty,
                    ):
                        # HarnessEvent → SSE 文本
                        yield _harness_evt.to_sse()
                        # 捕获最终内容（从 REPLACE 或 DELTA 事件）
                        if _harness_evt.event.value == "replace":
                            _final_content = _harness_evt.data.get("text", _final_content)
                        elif _harness_evt.event.value == "delta":
                            _final_content += _harness_evt.data.get("text", "")

                    _fast_msg = Message(
                        conversation_id=conv_id,
                        role=MessageRole.ASSISTANT,
                        content=_final_content,
                        metadata_={"skill_id": req.selected_skill_id, "studio_scope": "skill_studio"} if req.selected_skill_id else {"studio_scope": "skill_studio"},
                    )
                    db.add(_fast_msg)
                    _msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
                    if _msg_count <= 2:
                        conv.title = req.content[:60]
                    db.commit()
                    yield _sse("done", {"message_id": _fast_msg.id, "metadata": {}})
                    return

            # --- G2: Chat 主路径通过 AgentRuntime 执行 ---
            from app.harness.runtime import agent_runtime
            from app.harness.adapters import build_chat_request as _build_chat_req

            _status_queue: asyncio.Queue = asyncio.Queue()

            async def _on_status(stage: str):
                await _status_queue.put(("status", stage))

            yield _sse("status", {"stage": "preparing"})

            _chat_req = _build_chat_req(
                user_id=current_user_id,
                workspace_id=conv.workspace_id or 0,
                conversation_id=conv_id,
                user_message=req.content,
                stream=True,
            )

            # 启动 runtime.run 作为 task，同时 drain status queue
            _runtime_events: list = []
            _runtime_gen = agent_runtime.run(
                _chat_req, db, conv,
                active_skill_ids=req.active_skill_ids,
                force_skill_id=req.force_skill_id,
                on_status=_on_status,
            )

            # 包装 runtime_gen 为 task + queue 驱动模式
            _result_queue: asyncio.Queue = asyncio.Queue()
            _runtime_done = False

            async def _drain_runtime():
                nonlocal _runtime_done
                try:
                    async for item in _runtime_gen:
                        await _result_queue.put(item)
                except Exception as e:
                    await _result_queue.put(("__error__", e))
                finally:
                    _runtime_done = True
                    await _result_queue.put(None)  # sentinel

            _drain_task = asyncio.ensure_future(_drain_runtime())

            response = ""
            tool_meta: dict = {}
            prep = None
            skill_name = None

            while True:
                try:
                    item = await asyncio.wait_for(_result_queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # drain status events while waiting
                    while not _status_queue.empty():
                        _evt_type, _evt_stage = _status_queue.get_nowait()
                        yield _sse("status", {"stage": _evt_stage})
                    yield _SSE_KEEPALIVE
                    continue

                if item is None:
                    break  # sentinel — runtime done

                # Drain any pending status events
                while not _status_queue.empty():
                    _evt_type, _evt_stage = _status_queue.get_nowait()
                    yield _sse("status", {"stage": _evt_stage})

                if isinstance(item, tuple):
                    key, data = item
                    if key == "__result__":
                        response = data["response"]
                        tool_meta = data["tool_meta"]
                        prep = data["prep"]
                        skill_name = prep.skill_name if prep else None
                    elif key == "__error__":
                        raise data
                elif isinstance(item, dict):
                    evt = item.get("event", "")
                    evt_data = item.get("data", {})

                    if evt == "early_return":
                        # early return: 直接输出结果并结束
                        response_text = evt_data.get("response", "")
                        early_meta = evt_data.get("metadata", {})
                        llm_usage = early_meta.pop("llm_usage", {}) if isinstance(early_meta, dict) else {}
                        msg_metadata = {
                            "skill_id": req.selected_skill_id or conv.skill_id,
                            "skill_name": None,
                            "model_id": llm_usage.get("model_id"),
                            "input_tokens": llm_usage.get("input_tokens", 0),
                            "output_tokens": llm_usage.get("output_tokens", 0),
                            **early_meta,
                        }
                        if _ws_type_stream == "skill_studio":
                            msg_metadata["studio_scope"] = "skill_studio"
                        assistant_msg = Message(
                            conversation_id=conv_id,
                            role=MessageRole.ASSISTANT,
                            content=response_text,
                            metadata_=msg_metadata,
                        )
                        db.add(assistant_msg)
                        msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
                        if msg_count <= 2 and not _is_opencode_conv:
                            conv.title = req.content[:60]
                        db.commit()
                        yield _sse("delta", {"text": response_text})
                        yield _sse("done", {
                            "message_id": assistant_msg.id,
                            "metadata": msg_metadata,
                        })
                        # 等待 drain_task 结束
                        await _drain_task
                        return

                    elif evt == "error":
                        yield _sse("error", evt_data)
                        await _drain_task
                        return

                    else:
                        # 透传所有 SSE 事件
                        yield _sse(evt, evt_data)

            # 等待 drain_task 完成
            await _drain_task

            # --- 持久化 + done ---
            msg_metadata = {
                "skill_id": req.selected_skill_id or conv.skill_id,
                "skill_name": skill_name,
                **tool_meta,
            }
            if _ws_type_stream == "skill_studio":
                msg_metadata["studio_scope"] = "skill_studio"

            assistant_msg = Message(
                conversation_id=conv_id,
                role=MessageRole.ASSISTANT,
                content=response,
                metadata_=msg_metadata,
            )
            db.add(assistant_msg)

            msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
            if msg_count <= 2 and not _is_opencode_conv:
                conv.title = req.content[:60]

            db.commit()

            # Token usage estimation
            estimated_input_tokens = 0
            estimated_output_tokens = len(response) // 2
            model_context_limit = 32000
            if prep:
                total_input_chars = sum(len(m.get("content") or "") for m in prep.llm_messages)
                estimated_input_tokens = total_input_chars // 2
                model_context_limit = prep.model_config.get("context_window", 32000)

            # 记录 Skill 执行度量
            _skill_id = prep.skill_id if prep else (conv.skill_id if conv.skill_id else None)
            if _skill_id:
                try:
                    skill_engine.record_execution(
                        db,
                        skill_id=_skill_id,
                        conversation_id=conv_id,
                        user_id=current_user_id,
                        success=_stream_success,
                        duration_ms=int((_time_mod.monotonic() - _stream_start) * 1000),
                        tool_call_count=_tool_call_count,
                        tool_error_count=_tool_error_count,
                        token_usage={"input_tokens": estimated_input_tokens, "output_tokens": estimated_output_tokens},
                        error_type=_stream_error_type,
                    )
                except Exception:
                    logger.warning("Failed to record skill execution", exc_info=True)

            yield _sse("done", {
                "message_id": assistant_msg.id,
                "metadata": msg_metadata,
                "token_usage": {
                    "input_tokens": estimated_input_tokens,
                    "output_tokens": estimated_output_tokens,
                    "estimated_context_used": estimated_input_tokens + estimated_output_tokens,
                    "context_limit": model_context_limit,
                },
            })

        except Exception as e:
            _stream_success = False
            _stream_error_type = _classify_error(e) if '_classify_error' in dir() else "unknown"
            import traceback
            tb_str = traceback.format_exc()
            traceback.print_exc()
            error_type = _classify_error(e)
            error_msg = str(e) or f"{type(e).__name__} (see server log)"
            logger.error(f"Stream error [{type(e).__name__}]: {error_msg}\n{tb_str}")
            yield _sse("error", {
                "message": error_msg,
                "error_type": error_type,
                "retryable": error_type in ("network", "rate_limit"),
            })
        finally:
            if _owns_session:
                db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.post("/{conv_id}/messages/{msg_id}/save-as-knowledge")
def save_message_as_knowledge(
    conv_id: int,
    msg_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将 assistant 消息内容沉淀为知识条目（Skill 产出通道）。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    msg = db.get(Message, msg_id)
    if not msg or msg.conversation_id != conv_id:
        raise HTTPException(404, "Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise HTTPException(400, "Only assistant messages can be saved as knowledge")

    # 构建标题：取消息前60个字符
    title = msg.content[:60].strip()
    if len(msg.content) > 60:
        title += "..."

    entry = KnowledgeEntry(
        title=title,
        content=msg.content,
        category="experience",
        industry_tags=[],
        platform_tags=[],
        topic_tags=[],
        created_by=user.id,
        department_id=user.department_id,
        source_type="skill_output",
        capture_mode="skill_output",
    )
    db.add(entry)
    db.flush()

    from app.services.knowledge_service import submit_knowledge
    entry = submit_knowledge(db, entry)

    return {
        "knowledge_id": entry.id,
        "status": entry.status.value,
        "review_level": entry.review_level,
        "auto_approved": entry.status.value == "approved",
        "taxonomy_code": entry.taxonomy_code,
        "taxonomy_board": entry.taxonomy_board,
    }


class RatingBody(BaseModel):
    rating: int  # 1=差 5=好


@router.post("/{conv_id}/messages/{msg_id}/rating")
def rate_message(
    conv_id: int,
    msg_id: int,
    body: RatingBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户对 assistant 消息评分，更新关联的 SkillExecutionLog.user_rating。"""
    if body.rating < 1 or body.rating > 5:
        raise HTTPException(400, "评分范围 1-5")

    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    msg = db.get(Message, msg_id)
    if not msg or msg.conversation_id != conv_id:
        raise HTTPException(404, "Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise HTTPException(400, "只能对 assistant 消息评分")

    # 查找对应的 SkillExecutionLog（同一 conv + skill + 最近时间）
    skill_id = (msg.metadata_ or {}).get("skill_id") or conv.skill_id
    if not skill_id:
        raise HTTPException(400, "该消息无关联 Skill")

    from app.models.skill import SkillExecutionLog
    log = (
        db.query(SkillExecutionLog)
        .filter(
            SkillExecutionLog.skill_id == skill_id,
            SkillExecutionLog.conversation_id == conv_id,
        )
        .order_by(SkillExecutionLog.created_at.desc())
        .first()
    )
    if not log:
        raise HTTPException(404, "未找到执行记录")

    log.user_rating = body.rating
    db.commit()
    return {"ok": True, "rating": body.rating}


@router.post("/{conv_id}/messages/upload")
async def upload_and_chat(
    conv_id: int,
    message: Optional[str] = Form(None),
    file: UploadFile = File(...),
    active_skill_ids: Optional[str] = Form(None),  # JSON array string e.g. "[1,2,3]"
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """在 chat 中上传文件，自动分类并生成 AI 回复（带分类建议）。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # M8: 文件大小限制 50MB
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024
    content_bytes = await file.read()
    if len(content_bytes) > _MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"文件大小超过限制（最大 50MB，实际 {len(content_bytes) / 1024 / 1024:.1f}MB）")

    # 保存文件
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1]
    saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    with open(saved_path, "wb") as f:
        f.write(content_bytes)

    # 提取文本（放入线程池，避免阻塞事件循环；kimi vision 等同步 IO 耗时较长）
    try:
        file_text = await asyncio.get_event_loop().run_in_executor(
            None, extract_text, saved_path
        )
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    # 运行自动分类
    from app.services.knowledge_classifier import classify
    cls_result = None
    try:
        cls_result = await classify(file_text, db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Classification failed: {e}")

    # 超长文本：用 FOE MapReduce 生成结构化摘要用于聊天上下文；原文完整保存到知识库
    _FOE_THRESHOLD = 2000
    chat_content = file_text  # 默认直接用原文
    foe_summary = None
    if len(file_text) > _FOE_THRESHOLD:
        try:
            from app.services.llm_gateway import llm_gateway
            from app.utils.file_parser import foe_summarize
            _cfg = llm_gateway.resolve_config(db, "conversation.title")
            foe_summary = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: foe_summarize(raw_text=file_text, llm_cfg=_cfg),
            )
            chat_content = foe_summary
        except Exception as _e:
            import logging as _log2
            _log2.getLogger(__name__).warning(f"FOE summarize failed, using raw text: {_e}")

    # 构建发送给 skill_engine 的消息（带分类 context）
    cls_context = ""
    if cls_result:
        cls_context = (
            f"\n\n[系统自动分类] 该文件已被识别为「{cls_result.taxonomy_path[-1] if cls_result.taxonomy_path else cls_result.taxonomy_code}」"
            f"（板块 {cls_result.taxonomy_board}，置信度 {cls_result.confidence:.0%}）"
            f"，建议归入知识库：{', '.join(cls_result.target_kb_ids)}"
        )

    # chat_content: FOE摘要（长文）或原文（短文），用于对话上下文
    # file_text: 原始完整文本，用于知识库存储，数据不丢失
    file_label = f"[文件: {file.filename}]" + (" [FOE摘要]" if foe_summary else "")
    user_text = f"{file_label}\n{chat_content}"
    if message:
        user_text = f"{message}\n\n{user_text}"
    user_text += cls_context

    # 并行：存 KB + 调 skill_engine
    import asyncio as _asyncio
    import logging as _logging

    # 提前取出 user 字段，避免 async task 中访问 detached ORM 对象
    _user_id = user.id
    _dept_id = user.department_id
    _foe_summary = foe_summary  # 捕获到闭包，避免外层变量被覆盖

    async def _save_to_kb():
        # 独立 session，失败不污染主事务
        from app.database import SessionLocal as _SessionLocal
        kb_db = _SessionLocal()
        try:
            kb_entry = KnowledgeEntry(
                title=file.filename or "上传文件",
                content=file_text,
                summary=_foe_summary,
                category=cls_result.taxonomy_board if cls_result else "experience",
                industry_tags=[],
                platform_tags=[],
                topic_tags=[],
                created_by=_user_id,
                department_id=_dept_id,
                source_type="chat_upload",
                capture_mode="chat_upload",
            )
            kb_db.add(kb_entry)
            kb_db.flush()
            from app.services.knowledge_service import submit_knowledge
            kb_entry = submit_knowledge(kb_db, kb_entry)
            kb_db.commit()

            # 同步跑文档理解，产出分类/命名/摘要供前端弹窗确认
            understanding_result = None
            try:
                from app.services.knowledge_understanding import understand_document
                profile = await understand_document(
                    knowledge_id=kb_entry.id,
                    content=file_text,
                    filename=file.filename or "",
                    file_type="",
                    db=kb_db,
                )
                if profile and profile.understanding_status in ("success", "partial"):
                    # 向后兼容：同步主表
                    if profile.display_title:
                        kb_entry.ai_title = profile.display_title
                    if profile.summary_short:
                        kb_entry.ai_summary = profile.summary_short
                    kb_db.commit()

                    understanding_result = {
                        "profile_id": profile.id,
                        "display_title": profile.display_title,
                        "document_type": profile.document_type,
                        "summary_short": profile.summary_short,
                        "content_tags": profile.content_tags,
                        "desensitization_level": profile.desensitization_level,
                        "system_id": profile.system_id,
                        "quality_score": profile.title_confidence,
                    }
            except Exception as _ue:
                _logging.getLogger(__name__).warning(f"Chat upload understanding failed: {_ue}")

            return {"entry_id": kb_entry.id, "understanding": understanding_result}
        except Exception as e:
            kb_db.rollback()
            _logging.getLogger(__name__).warning(f"KB auto-save failed: {e}")
            return None
        finally:
            kb_db.close()

    # 先把用户消息存入 DB 并 commit，切换 tab 时消息不会丢失
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=user_text,
        metadata_={"file_upload": True, "filename": file.filename},
    )
    db.add(user_msg)
    db.commit()

    # 并行执行 KB 存储和 skill_engine（current_message="" 避免重复评估）
    # .md 文件（Skill 文件）不存入知识库
    _is_skill_file = (file.filename or "").lower().endswith(".md")
    kb_task = _asyncio.create_task(_save_to_kb()) if not _is_skill_file else None
    try:
        parsed_skill_ids = None
        if active_skill_ids:
            import json as _j
            try:
                parsed_skill_ids = _j.loads(active_skill_ids)
            except Exception:
                parsed_skill_ids = None
        result = await skill_engine.execute(db, conv, user_text, user_id=user.id, active_skill_ids=parsed_skill_ids)
    except ValueError as e:
        raise HTTPException(503, str(e))
    kb_result = (await kb_task) if kb_task else None
    kb_entry_id = kb_result["entry_id"] if isinstance(kb_result, dict) else None
    understanding = kb_result.get("understanding") if isinstance(kb_result, dict) else None

    response, guide_meta = result

    # Extract token usage from guide_meta
    llm_usage = guide_meta.pop("llm_usage", {})

    # 保存 assistant 回复
    assistant_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        content=response,
        metadata_={
            "skill_id": conv.skill_id,
            "file_upload": True,
            "filename": file.filename,
            "kb_entry_id": kb_entry_id,
            "classification": cls_result.to_dict() if cls_result else None,
            "model_id": llm_usage.get("model_id"),
            "input_tokens": llm_usage.get("input_tokens", 0),
            "output_tokens": llm_usage.get("output_tokens", 0),
            **guide_meta,
        },
    )
    db.add(assistant_msg)

    if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
        conv.title = f"[文件] {file.filename}"[:60]

    db.commit()

    skill_name = None
    if conv.skill_id:
        from app.models.skill import Skill as SkillModel
        sk = db.get(SkillModel, conv.skill_id)
        skill_name = sk.name if sk else None

    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": response,
        "skill_id": conv.skill_id,
        "skill_name": skill_name,
        "classification": cls_result.to_dict() if cls_result else None,
        "filename": file.filename,
        "understanding": understanding,
    }


@router.post("/{conv_id}/messages/upload-stream")
async def upload_stream_message(
    request: Request,
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming endpoint for file upload: upload + parse + stream LLM response.

    支持两种上传模式：
    - 单文件：字段名 `file`（UploadFile）
    - 多文件拼盘：字段名 `file_<key>`，每个 key 对应 manifest data_source 中的 uploaded_file slot
    """
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # 动态解析 multipart form，兼容单文件和多文件拼盘
    form = await request.form()
    message: Optional[str] = form.get("message")
    active_skill_ids: Optional[str] = form.get("active_skill_ids")

    # 收集所有文件：单文件走 `file` 字段，多文件走 `file_<key>` 字段
    # uploaded_files: list of (key, filename, bytes)
    uploaded_files: list[tuple[str, str, bytes]] = []
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # M8: 50MB 限制
    for field_name, field_value in form.multi_items():
        if not isinstance(field_value, StarletteUploadFile):
            continue
        if field_name == "file":
            content = await field_value.read()
            if len(content) > _MAX_UPLOAD_SIZE:
                raise HTTPException(413, f"文件 '{field_value.filename}' 超过 50MB 限制")
            uploaded_files.append(("file", field_value.filename or "", content))
        elif field_name.startswith("file_"):
            key = field_name[5:]  # strip "file_" prefix
            content = await field_value.read()
            if len(content) > _MAX_UPLOAD_SIZE:
                raise HTTPException(413, f"文件 '{field_value.filename}' 超过 50MB 限制")
            uploaded_files.append((key, field_value.filename or "", content))

    if not uploaded_files:
        raise HTTPException(400, "至少需要上传一个文件")

    # 兼容原有单文件变量名，多文件时取第一个作为代表（用于 title 等展示）
    _primary_key, file_filename, file_content_bytes = uploaded_files[0]

    # Capture user.id as a plain int before the generator closes over it —
    # the ORM User object becomes detached once the session flushes/expires.
    current_user_id = user.id

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    _outer_db2 = db

    async def event_generator():
        import time as _time_mod2
        _stream_start2 = _time_mod2.monotonic()

        from app.database import SessionLocal
        _owns_session = False
        try:
            _outer_db2.execute(text("SELECT 1"))
            db = _outer_db2
        except Exception:
            db = SessionLocal()
            _owns_session = True
        try:
            # 1. Upload phase — 保存所有文件到磁盘
            yield _sse("status", {"stage": "uploading"})

            os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

            # saved_files: list of (key, original_filename, saved_path)
            saved_files: list[tuple[str, str, str]] = []
            for _key, _fname, _bytes in uploaded_files:
                ext = os.path.splitext(_fname)[1]
                _path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
                with open(_path, "wb") as f:
                    f.write(_bytes)
                saved_files.append((_key, _fname, _path))

            # 2. Parse phase — 逐文件提取文本
            yield _sse("status", {"stage": "parsing"})

            from app.services.knowledge_classifier import classify
            _FOE_THRESHOLD = 2000

            # 每个文件独立解析，结果收集后拼装
            # parsed_parts: list of (key, filename, raw_text, chat_content, foe_summary)
            parsed_parts = []
            primary_cls_result = None  # 用第一个文件的分类结果

            for _key, _fname, _path in saved_files:
                try:
                    _raw = extract_text(_path)
                except ValueError as e:
                    os.unlink(_path)
                    yield _sse("error", {"message": f"[{_fname}] {e}", "error_type": "server_error", "retryable": False})
                    return

                _foe = None
                _chat = _raw
                if len(_raw) > _FOE_THRESHOLD:
                    yield _sse("status", {"stage": "summarizing"})
                    try:
                        from app.utils.file_parser import foe_summarize
                        _cfg = llm_gateway.resolve_config(db, "conversation.title")
                        _foe = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda r=_raw: foe_summarize(raw_text=r, llm_cfg=_cfg),
                        )
                        _chat = _foe
                    except Exception:
                        pass

                # Classification — 只对第一个文件做，避免多次调用
                if primary_cls_result is None:
                    try:
                        primary_cls_result = await classify(_raw, db)
                    except Exception:
                        pass

                parsed_parts.append((_key, _fname, _raw, _chat, _foe))

            # 多文件时 foe_summary 取第一个文件的（用于 KB 保存）
            foe_summary = parsed_parts[0][4] if parsed_parts else None
            # 主文件文本（第一个，用于单文件兼容路径）
            file_text = parsed_parts[0][2] if parsed_parts else ""
            cls_result = primary_cls_result

            # Build user text — 多文件拼装
            file_segments = []
            for _key, _fname, _raw, _chat, _foe in parsed_parts:
                _label = f"[文件({_key}): {_fname}]" + (" [FOE摘要]" if _foe else "")
                file_segments.append(f"{_label}\n{_chat}")
            combined_file_text = "\n\n---\n\n".join(file_segments)

            cls_context = ""
            if cls_result:
                cls_context = (
                    f"\n\n[系统自动分类] 该文件已被识别为「{cls_result.taxonomy_path[-1] if cls_result.taxonomy_path else cls_result.taxonomy_code}」"
                    f"（板块 {cls_result.taxonomy_board}，置信度 {cls_result.confidence:.0%}）"
                    f"，建议归入知识库：{', '.join(cls_result.target_kb_ids)}"
                )

            user_text = combined_file_text
            if message:
                user_text = f"{message}\n\n{user_text}"
            user_text += cls_context

            # Save user message immediately so it survives if SSE is dropped mid-stream
            _filenames = [fn for _, fn, *_ in parsed_parts]
            user_msg = Message(
                conversation_id=conv_id,
                role=MessageRole.USER,
                content=user_text,
                metadata_={
                    "file_upload": True,
                    "filename": file_filename,  # 主文件（兼容旧字段）
                    "filenames": _filenames,     # 多文件完整列表
                },
            )
            db.add(user_msg)
            db.commit()

            # KB save in background — 跳过 .md 文件（Skill 文件不入知识库）
            _user_id = user.id
            _dept_id = user.department_id
            _foe_summary = foe_summary
            _is_skill_file = file_filename.lower().endswith(".md")

            async def _save_to_kb():
                from app.database import SessionLocal as _SessionLocal
                kb_db = _SessionLocal()
                try:
                    kb_entry = KnowledgeEntry(
                        title=file_filename or "上传文件",
                        content=file_text,
                        summary=_foe_summary,
                        category=cls_result.taxonomy_board if cls_result else "experience",
                        industry_tags=[], platform_tags=[], topic_tags=[],
                        created_by=_user_id, department_id=_dept_id,
                        source_type="chat_upload", capture_mode="chat_upload",
                    )
                    kb_db.add(kb_entry)
                    kb_db.flush()
                    from app.services.knowledge_service import submit_knowledge
                    kb_entry = submit_knowledge(kb_db, kb_entry)
                    kb_db.commit()
                    return kb_entry.id
                except Exception:
                    kb_db.rollback()
                    return None
                finally:
                    kb_db.close()

            kb_task = asyncio.create_task(_save_to_kb()) if not _is_skill_file else None

            # 3. Prepare + stream LLM
            yield _sse("status", {"stage": "preparing"})

            parsed_skill_ids = None
            if active_skill_ids:
                try:
                    parsed_skill_ids = json.loads(active_skill_ids)
                except Exception:
                    pass

            _status_queue2: asyncio.Queue = asyncio.Queue()

            async def _on_status2(stage: str):
                await _status_queue2.put(stage)

            _prep_task2 = asyncio.ensure_future(skill_engine.prepare(
                db, conv, user_text,
                user_id=current_user_id,
                active_skill_ids=parsed_skill_ids,
                on_status=_on_status2,
            ))
            while not _prep_task2.done():
                try:
                    _s = await asyncio.wait_for(_status_queue2.get(), timeout=10)
                    yield _sse("status", {"stage": _s})
                except asyncio.TimeoutError:
                    yield _SSE_KEEPALIVE
            prep = _prep_task2.result()

            skill_name = prep.skill_name

            if prep.early_return is not None:
                response_text, early_meta = prep.early_return
                llm_usage = early_meta.pop("llm_usage", {})
                _title_label = file_filename if len(uploaded_files) == 1 else f"{file_filename} 等{len(uploaded_files)}个文件"
                msg_metadata = {
                    "skill_id": conv.skill_id,
                    "skill_name": skill_name,
                    "file_upload": True,
                    "filename": file_filename,
                    "filenames": _filenames,
                    "model_id": llm_usage.get("model_id"),
                    "input_tokens": llm_usage.get("input_tokens", 0),
                    "output_tokens": llm_usage.get("output_tokens", 0),
                    **early_meta,
                }
                assistant_msg = Message(
                    conversation_id=conv_id, role=MessageRole.ASSISTANT,
                    content=response_text, metadata_=msg_metadata,
                )
                db.add(assistant_msg)
                if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
                    conv.title = f"[文件] {_title_label}"[:60]
                    db.add(conv)
                db.commit()
                if kb_task:
                    await kb_task
                yield _sse("delta", {"text": response_text})
                yield _sse("done", {"message_id": assistant_msg.id, "metadata": msg_metadata})
                return

            # Stream LLM
            yield _sse("status", {"stage": "generating", "skill_name": skill_name})

            full_content = ""
            full_thinking = ""
            block_idx = 0
            current_block_type = None
            native_tool_calls: list[dict] = []

            _llm_stream2 = llm_gateway.chat_stream_typed(
                model_config=prep.model_config,
                messages=prep.llm_messages,
                tools=prep.tools_schema or None,
            )
            async for item in _stream_with_keepalive(_llm_stream2):
                if isinstance(item, str):
                    yield item
                    continue
                chunk_type, chunk_data = item
                if chunk_type == "thinking":
                    full_thinking += chunk_data
                    if current_block_type != "thinking":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "thinking"})
                        current_block_type = "thinking"
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                elif chunk_type == "tool_call":
                    native_tool_calls.append(chunk_data)
                else:
                    if current_block_type != "text":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "text"})
                        current_block_type = "text"
                    full_content += chunk_data
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                    yield _sse("delta", {"text": chunk_data})

            if current_block_type is not None:
                yield _sse("content_block_stop", {"index": block_idx})

            response = full_content
            tool_meta: dict = {}

            # Agent Loop: 原生 function calling 或文本 fallback
            if native_tool_calls or "```tool_call" in response:
                from app.models.skill import Skill as SkillModel
                skill_obj = db.get(SkillModel, prep.skill_id) if prep.skill_id else None
                yield _sse("status", {"stage": "tool_calling"})
                _next_block_idx = block_idx + (1 if current_block_type is not None else 0)
                async for item in skill_engine._handle_tool_calls_stream(
                    db, skill_obj, response, prep.llm_messages, prep.model_config, current_user_id,
                    tools_schema=prep.tools_schema or None,
                    native_tool_calls=native_tool_calls or None,
                    start_block_idx=_next_block_idx,
                    thinking_content=full_thinking,
                ):
                    if isinstance(item, tuple):
                        response, tool_meta = item
                    else:
                        yield _sse(item["event"], item["data"])
                yield _sse("replace", {"text": response})

            kb_entry_id = (await kb_task) if kb_task else None

            msg_metadata = {
                "skill_id": conv.skill_id,
                "skill_name": skill_name,
                "file_upload": True,
                "filename": file_filename,
                "filenames": _filenames,
                "kb_entry_id": kb_entry_id,
                **tool_meta,
            }

            assistant_msg = Message(
                conversation_id=conv_id, role=MessageRole.ASSISTANT,
                content=response, metadata_=msg_metadata,
            )
            db.add(assistant_msg)
            _title_label = file_filename if len(uploaded_files) == 1 else f"{file_filename} 等{len(uploaded_files)}个文件"
            if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
                conv.title = f"[文件] {_title_label}"[:60]
                db.add(conv)
            db.commit()

            total_input_chars = sum(len(m.get("content") or "") for m in prep.llm_messages)
            estimated_input_tokens = total_input_chars // 2
            estimated_output_tokens = len(response) // 2
            model_context_limit = prep.model_config.get("context_window", 32000)

            # Gap 1: 记录 Skill 执行度量（文件上传流）
            if prep and prep.skill_id:
                try:
                    skill_engine.record_execution(
                        db,
                        skill_id=prep.skill_id,
                        conversation_id=conv_id,
                        user_id=current_user_id,
                        success=True,
                        duration_ms=int((_time_mod2.monotonic() - _stream_start2) * 1000),
                        token_usage={"input_tokens": estimated_input_tokens, "output_tokens": estimated_output_tokens},
                    )
                except Exception:
                    logger.warning("Failed to record skill execution (file)", exc_info=True)

            yield _sse("done", {
                "message_id": assistant_msg.id,
                "metadata": msg_metadata,
                "token_usage": {
                    "input_tokens": estimated_input_tokens,
                    "output_tokens": estimated_output_tokens,
                    "estimated_context_used": estimated_input_tokens + estimated_output_tokens,
                    "context_limit": model_context_limit,
                },
            })

        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            traceback.print_exc()
            error_type = _classify_error(e)
            error_msg = str(e) or f"{type(e).__name__} (see server log)"
            logger.error(f"Stream error [{type(e).__name__}]: {error_msg}\n{tb_str}")
            yield _sse("error", {
                "message": error_msg,
                "error_type": error_type,
                "retryable": error_type in ("network", "rate_limit"),
            })
        finally:
            if _owns_session:
                db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConversationPatch(BaseModel):
    title: Optional[str] = None
    skill_id: Optional[int] = None  # None = keep current; -1 = clear
    is_active: Optional[bool] = None


@router.patch("/{conv_id}")
def patch_conversation(
    conv_id: int,
    req: ConversationPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    if req.title is not None:
        conv.title = req.title[:60]
    if req.skill_id is not None:
        conv.skill_id = None if req.skill_id == -1 else req.skill_id
    if req.is_active is not None:
        conv.is_active = req.is_active
    db.commit()
    return {"ok": True, "title": conv.title, "skill_id": conv.skill_id}


@router.delete("/{conv_id}")
def delete_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    conv.is_active = False
    db.commit()
    return {"ok": True}


# ── Skill Studio: 按任务压缩对话上下文 ────────────────────────────────────────

class CompressByTaskRequest(BaseModel):
    skill_id: int
    task_id: str
    rollup: str


@router.post("/{conv_id}/messages/compress-by-task")
def compress_by_task(
    conv_id: int,
    req: CompressByTaskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Skill Studio 专用：按已完成任务压缩对话，返回 rollup 后消息集。

    将该任务相关的多轮对话压缩为一条 rollup 摘要消息，保留最近活跃任务上下文。
    """
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # 返回压缩后的消息：rollup 摘要 + 下一步引导
    return {
        "messages": [
            {"role": "assistant", "text": req.rollup},
            {"role": "assistant", "text": "接下来继续下一个任务。"},
        ]
    }
