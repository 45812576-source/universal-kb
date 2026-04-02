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


async def _stream_with_keepalive(agen):
    """Wrap an async generator: yield items as they arrive;
    if no item arrives within _KEEPALIVE_INTERVAL seconds, yield a keepalive ping.
    Prevents nginx / Next.js proxy from closing idle SSE connections during
    long LLM thinking phases."""
    it = agen.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            try:
                item = await asyncio.wait_for(asyncio.shield(pending), timeout=_KEEPALIVE_INTERVAL)
                pending = asyncio.ensure_future(it.__anext__())
                yield item
            except asyncio.TimeoutError:
                yield _SSE_KEEPALIVE  # keepalive ping, loop continues
            except StopAsyncIteration:
                break
    finally:
        pending.cancel()
        try:
            await pending
        except (asyncio.CancelledError, StopAsyncIteration):
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

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("消息内容不能为空")
        return v


class ConversationCreate(BaseModel):
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None


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
        for m in conv.messages
    ]


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
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=req.content,
        metadata_=_user_msg_meta if _user_msg_meta else {},
    )
    db.add(user_msg)
    db.commit()

    # skill_studio 快速路径：用 system_context 直接对话，跳过 skill_engine
    if _ws_obj and _ws_obj.workspace_type == "skill_studio" and _ws_obj.system_context:
        _history = (
            db.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.created_at)
            .all()
        )
        _llm_msgs = [{"role": "system", "content": _ws_obj.system_context}]
        for _m in _history:
            _llm_msgs.append({"role": "user" if _m.role == MessageRole.USER else "assistant", "content": _m.content or ""})
        _model_cfg = llm_gateway.resolve_config(db, "conversation.main", getattr(_ws_obj, "model_config_id", None))
        try:
            _resp_text, _ = await llm_gateway.chat(_model_cfg, _llm_msgs)
        except Exception as e:
            raise HTTPException(503, f"AI 服务暂时不可用，请稍后重试")
        _assistant_msg = Message(
            conversation_id=conv_id,
            role=MessageRole.ASSISTANT,
            content=_resp_text,
            metadata_={},
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
            "skill_id": None,
            "skill_name": None,
            "metadata": {},
        }

    # Execute skill engine
    try:
        result = await skill_engine.execute(db, conv, req.content, user_id=user.id, active_skill_ids=req.active_skill_ids, force_skill_id=req.force_skill_id)
    except ValueError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(503, f"AI 服务暂时不可用，请稍后重试")

    # skill_engine always returns (content, meta_dict)
    response, guide_meta = result

    # Resolve skill name
    skill_name = None
    if conv.skill_id:
        from app.models.skill import Skill as SkillModel
        sk = db.get(SkillModel, conv.skill_id)
        skill_name = sk.name if sk else None

    # Extract token usage from guide_meta
    llm_usage = guide_meta.pop("llm_usage", {})
    msg_metadata = {
        "skill_id": conv.skill_id,
        "skill_name": skill_name,
        "model_id": llm_usage.get("model_id"),
        "input_tokens": llm_usage.get("input_tokens", 0),
        "output_tokens": llm_usage.get("output_tokens", 0),
        **guide_meta,
    }

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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming endpoint: same logic as send_message but streams LLM output."""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # 沙盒工作台 guard：在 commit user message 之前检查，避免无关消息入库
    if conv.workspace_id:
        from app.models.workspace import Workspace as WsModel
        _ws_pre = db.get(WsModel, conv.workspace_id)
        if _ws_pre and _ws_pre.workspace_type == "sandbox" and not req.force_skill_id:
            raise HTTPException(400, "沙盒测试工作台仅用于测试指定 Skill，请从技能列表发起测试")

    # Persist user message — commit immediately so it survives if SSE is dropped mid-stream
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=req.content,
        metadata_={"skill_id": req.selected_skill_id} if req.selected_skill_id else {},
    )
    db.add(user_msg)
    db.commit()

    # Capture user.id as a plain int before the generator closes over it —
    # the ORM User object becomes detached once the session flushes/expires.
    current_user_id = user.id

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def event_generator():
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

            # --- skill_studio 路径：调用 studio_agent orchestrator ---
            if _skip_pev and conv.workspace_id:
                from app.models.workspace import Workspace as WsModel3
                _ws_fast = db.get(WsModel3, conv.workspace_id)
                if _ws_fast and _ws_fast.workspace_type == "skill_studio":
                    from app.services.studio_agent import run_stream as _studio_run_stream
                    logger.info(
                        f"[studio_agent] conv={conv_id} skill={req.selected_skill_id} "
                        f"dirty={req.editor_is_dirty}"
                    )

                    # 构建历史消息（按 selected_skill_id 隔离，每个 skill 最多 30 轮 = 60 条）
                    _STUDIO_HISTORY_ROUNDS = 30
                    _sid = req.selected_skill_id
                    if _sid:
                        # 取该 skill 下最近 60 条（user 消息 metadata_.skill_id 匹配，assistant 消息紧跟其后）
                        # 简化方案：取 conv 内所有消息，过滤出属于该 skill 的对话对
                        _all_msgs = (
                            db.query(Message)
                            .filter(Message.conversation_id == conv_id)
                            .order_by(Message.created_at)
                            .all()
                        )
                        # 按 user message 的 skill_id 标记筛选对话对
                        _skill_pairs: list[dict] = []
                        _pending_user: dict | None = None
                        for _m in _all_msgs:
                            if _m.role == MessageRole.USER:
                                _meta = _m.metadata_ or {}
                                if _meta.get("skill_id") == _sid:
                                    _pending_user = {"role": "user", "content": _m.content or ""}
                                else:
                                    _pending_user = None
                            elif _m.role == MessageRole.ASSISTANT and _pending_user is not None:
                                _asst_content = (_m.content or "").strip()
                                if _asst_content:
                                    _skill_pairs.append(_pending_user)
                                    _skill_pairs.append({"role": "assistant", "content": _asst_content})
                                _pending_user = None
                        # 最多保留最近 30 轮
                        _llm_history = _skill_pairs[-(_STUDIO_HISTORY_ROUNDS * 2):]
                    else:
                        # 没有选中 skill 时，取全局最近 30 轮（无 skill_id 过滤）
                        _all_msgs = (
                            db.query(Message)
                            .filter(Message.conversation_id == conv_id)
                            .order_by(Message.created_at.desc())
                            .limit(_STUDIO_HISTORY_ROUNDS * 2)
                            .all()
                        )
                        _llm_history = [
                            {"role": "user" if _m.role == MessageRole.USER else "assistant",
                             "content": (_m.content or "").strip() or "(empty)"}
                            for _m in reversed(_all_msgs)
                            if (_m.content or "").strip()
                        ]

                    _fast_model = llm_gateway.resolve_config(db, "conversation.main", getattr(_ws_fast, "model_config_id", None))
                    yield _sse("status", {"stage": "preparing"})

                    # 查询可用工具列表，供 AI 推荐绑定
                    from app.models.tool import ToolRegistry, ToolType
                    _pub_tools = (
                        db.query(ToolRegistry)
                        .filter(ToolRegistry.status == "published")
                        .limit(50)
                        .all()
                    )
                    if _pub_tools:
                        _tools_text = "\n".join(
                            f"  - [{t.id}] {t.display_name or t.name}（{t.tool_type.value}）：{t.description or '无描述'}"
                            for t in _pub_tools
                        )
                    else:
                        _tools_text = "（暂无已注册工具）"

                    # 查询当前 skill 的附属文件列表 + 读取文件内容注入
                    _source_files: list[dict] = []
                    _source_files_content: str = ""
                    if req.selected_skill_id:
                        from app.models.skill import Skill as SkillModel
                        _cur_skill = db.get(SkillModel, req.selected_skill_id)
                        if _cur_skill:
                            _source_files = list(_cur_skill.source_files or [])
                            if _source_files:
                                from app.services.skill_engine import _read_source_files
                                _source_files_content = _read_source_files(
                                    req.selected_skill_id, _source_files
                                )

                    # 查询 memo 上下文
                    _memo_ctx = None
                    if req.selected_skill_id:
                        from app.services.skill_memo_service import get_memo as _get_memo
                        _memo_ctx = _get_memo(db, req.selected_skill_id)

                    yield _sse("status", {"stage": "generating"})

                    _final_content = ""
                    async for _item in _stream_with_keepalive(
                        _studio_run_stream(
                            db=db,
                            conv_id=conv_id,
                            workspace_system_context=_ws_fast.system_context or "",
                            history_messages=_llm_history,
                            user_message=req.content,
                            model_config=_fast_model,
                            selected_skill_id=req.selected_skill_id,
                            editor_prompt=req.editor_prompt,
                            editor_is_dirty=req.editor_is_dirty,
                            available_tools=_tools_text,
                            source_files=_source_files,
                            source_files_content=_source_files_content,
                            selected_source_filename=req.selected_source_filename,
                            memo_context=_memo_ctx,
                        )
                    ):
                        if isinstance(_item, str):
                            # keepalive ping
                            yield _item
                            continue
                        _evt, _data = _item
                        if _evt == "__full_content__":
                            _final_content = _data.get("text", "")
                        else:
                            yield _sse(_evt, _data)

                    _fast_msg = Message(
                        conversation_id=conv_id,
                        role=MessageRole.ASSISTANT,
                        content=_final_content,
                        metadata_={"skill_id": req.selected_skill_id} if req.selected_skill_id else {},
                    )
                    db.add(_fast_msg)
                    _msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
                    if _msg_count <= 2:
                        conv.title = req.content[:60]
                    db.commit()
                    yield _sse("done", {"message_id": _fast_msg.id, "metadata": {}})
                    return

            # --- Prepare phase (skill matching, knowledge, prompt) ---
            # 用 asyncio.Queue + keepalive 循环保护 prepare 阶段：
            # prepare 内部每完成一个子阶段就 put 一个 status，主循环取出来 yield 给前端；
            # 超过 10s 没有进展则发送 keepalive ping，防止 nginx 等代理超时断连。
            _status_queue: asyncio.Queue = asyncio.Queue()

            async def _on_status(stage: str):
                await _status_queue.put(("status", stage))

            yield _sse("status", {"stage": "preparing"})
            _prep_task = asyncio.ensure_future(skill_engine.prepare(
                db, conv, req.content,
                user_id=current_user_id,
                active_skill_ids=req.active_skill_ids,
                force_skill_id=req.force_skill_id,
                on_status=_on_status,
            ))

            # drain queue：持续取 status 事件并 yield，直到 prepare 完成
            while not _prep_task.done():
                try:
                    _evt_type, _evt_stage = await asyncio.wait_for(
                        _status_queue.get(), timeout=10
                    )
                    yield _sse("status", {"stage": _evt_stage})
                except asyncio.TimeoutError:
                    yield _SSE_KEEPALIVE  # ": ping\n\n" — 防止代理超时

            # 取出 prepare 结果（若有异常则抛出）
            prep = _prep_task.result()

            # Resolve skill name for metadata
            skill_name = prep.skill_name

            # Early return: prepare produced a non-streaming result
            if prep.early_return is not None:
                response_text, early_meta = prep.early_return
                llm_usage = early_meta.pop("llm_usage", {})
                msg_metadata = {
                    "skill_id": conv.skill_id,
                    "skill_name": skill_name,
                    "model_id": llm_usage.get("model_id"),
                    "input_tokens": llm_usage.get("input_tokens", 0),
                    "output_tokens": llm_usage.get("output_tokens", 0),
                    **early_meta,
                }
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
                return

            # --- Streaming LLM call ---
            yield _sse("status", {"stage": "generating", "skill_name": skill_name})

            full_content = ""
            full_thinking = ""
            block_idx = 0
            current_block_type = None  # "text" | "thinking" | None
            native_tool_calls: list[dict] = []

            _llm_stream = llm_gateway.chat_stream_typed(
                model_config=prep.model_config,
                messages=prep.llm_messages,
                tools=prep.tools_schema or None,
            )
            async for item in _stream_with_keepalive(_llm_stream):
                if isinstance(item, str):
                    # keepalive ping — pass raw bytes to keep connection alive
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
                    # 原生 function calling：收集，等第一轮 LLM 结束后统一处理
                    native_tool_calls.append(chunk_data)
                else:  # content
                    if current_block_type != "text":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "text"})
                        current_block_type = "text"
                    full_content += chunk_data
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                    yield _sse("delta", {"text": chunk_data})  # backward compat

            # Close last block
            if current_block_type is not None:
                yield _sse("content_block_stop", {"index": block_idx})

            # --- Post-processing (tool calls, structured output) ---
            response = full_content
            tool_meta: dict = {}

            # Structured output
            skill_version = prep.skill_version
            structured_output = None
            if skill_version and skill_version.output_schema:
                from app.services.skill_engine import SkillEngine
                parsed = SkillEngine._try_parse_structured_output(response)
                if parsed is not None:
                    structured_output = parsed
                    from app.services import prompt_compiler
                    response = prompt_compiler.render_structured_as_markdown(
                        skill_version.output_schema, parsed
                    )
                    yield _sse("replace", {"text": response})

            # Agent Loop: 原生 function calling 或文本 fallback
            if native_tool_calls or "```tool_call" in response:
                from app.models.skill import Skill as SkillModel
                skill_obj = db.get(SkillModel, prep.skill_id) if prep.skill_id else None
                yield _sse("status", {"stage": "tool_calling"})
                # When LLM returns only tool_calls (no text), block_idx=0 and current_block_type=None.
                # _handle_tool_calls_stream must start from 0 so it doesn't create sparse array holes.
                _next_block_idx = block_idx + (1 if current_block_type is not None else 0)
                async for item in skill_engine._handle_tool_calls_stream(
                    db, skill_obj, response, prep.llm_messages, prep.model_config, user.id,
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

            # PPT auto-execution
            if prep.skill_name == "pptx-generation" and "```python" in response:
                tool_meta = skill_engine._execute_pptx_code(response)
            if prep.skill_name == "pptx-generation" and not tool_meta and "```html" in response:
                tool_meta = skill_engine._execute_html_ppt(response)

            if structured_output is not None:
                tool_meta["structured_output"] = structured_output

            # Build metadata
            msg_metadata = {
                "skill_id": conv.skill_id,
                "skill_name": skill_name,
                **tool_meta,
            }

            # Persist assistant message
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

            # Estimate token usage for context warning
            total_input_chars = sum(len(m.get("content") or "") for m in prep.llm_messages)
            estimated_input_tokens = total_input_chars // 2  # rough char-to-token ratio for CJK
            estimated_output_tokens = len(response) // 2
            model_context_limit = prep.model_config.get("context_window", 32000)

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

    # 保存文件
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1]
    saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    content_bytes = await file.read()
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
    for field_name, field_value in form.multi_items():
        if not isinstance(field_value, StarletteUploadFile):
            continue
        if field_name == "file":
            content = await field_value.read()
            uploaded_files.append(("file", field_value.filename or "", content))
        elif field_name.startswith("file_"):
            key = field_name[5:]  # strip "file_" prefix
            content = await field_value.read()
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

    async def event_generator():
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
                    db, skill_obj, response, prep.llm_messages, prep.model_config, user.id,
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
