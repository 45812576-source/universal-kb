"""SkillStudioAgentProfile — Skill Studio 统一执行 Profile。

G3 核心交付：消除同步/流式双轨，统一走 SkillStudioAgentProfile。
- 同步入口: run_sync() — 内部调 run_stream 收集结果
- 流式入口: run_stream() — 唯一执行主链
- StudioSessionState 持久化到 HarnessSession.metadata

使用方：conversations.py 的 skill_studio 路径（同步 + 流式）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, AsyncIterator, Optional

from sqlalchemy.orm import Session

from app.harness.contracts import (
    AgentType,
    ArtifactType,
    HarnessArtifact,
    HarnessMemoryRef,
    HarnessRequest,
    HarnessResponse,
    HarnessRun,
    HarnessStep,
    RunStatus,
    StepType,
)
from app.harness.events import EventName, HarnessEvent, emit
from app.harness.session_store import SessionStore
from app.harness.gateway import get_session_store
from app.models.conversation import Conversation, Message, MessageRole

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# StudioSessionState 持久化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _persist_studio_state(
    store: SessionStore,
    session_key,
    state_dict: dict[str, Any],
    db: Optional[Session] = None,
) -> None:
    """将 StudioSessionState 写入 HarnessSession.metadata。"""
    session = store.get_session(session_key)
    if session:
        session.metadata["studio_state"] = state_dict
        if db:
            store._persist_event(db, "harness.studio.state_saved", "", session_key, {
                "session_id": session.session_id,
                "architect_phase": state_dict.get("architect_phase", ""),
                "scenario_type": state_dict.get("scenario_type", ""),
                "draft_readiness_score": state_dict.get("draft_readiness_score", 0),
            })


def _recover_studio_state(
    store: SessionStore,
    session_key,
) -> Optional[dict[str, Any]]:
    """从 HarnessSession.metadata 恢复 StudioSessionState。"""
    session = store.get_session(session_key)
    if session:
        return session.metadata.get("studio_state")
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 事件适配：studio_agent yield tuple -> HarnessEvent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# studio_agent 事件名 -> EventName 映射
_STUDIO_EVENT_MAP: dict[str, EventName] = {
    "status": EventName.STATUS,
    "route_status": EventName.ROUTE_STATUS,
    "assist_skills_status": EventName.ASSIST_SKILLS_STATUS,
    "architect_phase_status": EventName.ARCHITECT_PHASE_STATUS,
    "architect_question": EventName.ARCHITECT_QUESTION,
    "architect_phase_summary": EventName.ARCHITECT_PHASE_SUMMARY,
    "architect_structure": EventName.ARCHITECT_STRUCTURE,
    "architect_priority_matrix": EventName.ARCHITECT_PRIORITY_MATRIX,
    "architect_ooda_decision": EventName.ARCHITECT_OODA_DECISION,
    "architect_ready_for_draft": EventName.ARCHITECT_READY_FOR_DRAFT,
    "audit_summary": EventName.AUDIT_SUMMARY,
    "governance_card": EventName.GOVERNANCE_CARD,
    "staged_edit_notice": EventName.STAGED_EDIT_NOTICE,
    "content_block_start": EventName.CONTENT_BLOCK_START,
    "content_block_delta": EventName.CONTENT_BLOCK_DELTA,
    "content_block_stop": EventName.CONTENT_BLOCK_STOP,
    "delta": EventName.DELTA,
    "replace": EventName.REPLACE,
    "error": EventName.ERROR,
    "fallback_text": EventName.FALLBACK_TEXT,
    "done": EventName.DONE,
}

# studio_agent 内部事件（不透传给前端，仅用于内部状态管理）
_STUDIO_INTERNAL_EVENTS = {
    "__full_content__",
    "studio_reconciled_facts",
    "studio_direction_shift",
    "studio_file_need_status",
    "studio_repeat_blocked",
    "studio_state_update",
    "studio_route",         # 向后兼容旧前端，route_status 已覆盖
    "studio_audit",         # 内部审计，已通过 audit_summary 透传
    "studio_governance_action",  # 内部，已通过 governance_card 透传
}

# studio 专有事件（透传但不在 EventName 中的，作为 passthrough）
_STUDIO_PASSTHROUGH_EVENTS = {
    "studio_reconciled_facts",
    "studio_direction_shift",
    "studio_file_need_status",
    "studio_repeat_blocked",
    "studio_state_update",
}


def _map_studio_event(
    evt_name: str,
    evt_data: dict[str, Any],
    *,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[HarnessEvent]:
    """将 studio_agent 的 tuple 事件转换为 HarnessEvent。"""
    if evt_name == "__full_content__":
        return None  # 内部标记，不透传

    mapped = _STUDIO_EVENT_MAP.get(evt_name)
    if mapped:
        return emit(mapped, evt_data, run_id=run_id, session_id=session_id)

    # studio 专有事件 passthrough（保持原始事件名透传给前端）
    if evt_name in _STUDIO_PASSTHROUGH_EVENTS:
        # 使用 STATUS category 作为 fallback
        return HarnessEvent(
            event=EventName.STATUS,  # category fallback
            data={"_original_event": evt_name, **evt_data},
            run_id=run_id,
            session_id=session_id,
        )

    # 未知事件 — 记录日志但不丢弃
    logger.debug("Unknown studio event: %s", evt_name)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SkillStudioAgentProfile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SkillStudioAgentProfile:
    """Skill Studio 统一执行 Profile。

    职责:
    1. 创建 HarnessRun + HarnessStep
    2. 调用 studio_agent.run_stream（唯一执行路径）
    3. 将 studio 事件映射为 HarnessEvent
    4. 持久化 StudioSessionState 到 HarnessSession
    5. 记录 Architect Workflow 状态变更
    """

    def __init__(self, store: Optional[SessionStore] = None):
        self.store = store or get_session_store()

    async def run_stream(
        self,
        request: HarnessRequest,
        db: Session,
        conversation: Conversation,
        *,
        selected_skill_id: Optional[int] = None,
        editor_prompt: Optional[str] = None,
        editor_is_dirty: bool = False,
    ) -> AsyncIterator[HarnessEvent]:
        """流式执行 Skill Studio 请求 — 唯一主链。

        同步入口也应调用此方法收集结果。
        """
        # 1. 创建 session + run
        harness_session = self.store.create_or_get_session(
            request.session_key,
            agent_type=AgentType.SKILL_STUDIO,
            db=db,
        )
        run = HarnessRun(
            request_id=request.request_id,
            session_id=harness_session.session_id,
            session_key=request.session_key,
            agent_type=AgentType.SKILL_STUDIO,
        )
        self.store.create_run(run, db=db)
        self.store.update_run_status(run.run_id, RunStatus.RUNNING, db=db)

        yield emit(
            EventName.RUN_STARTED,
            {"run_id": run.run_id, "agent_type": "skill_studio"},
            run_id=run.run_id,
            session_id=harness_session.session_id,
        )

        # 2. 记录 context assembly step
        ctx_step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.CONTEXT_ASSEMBLED,
            seq=0,
            input_summary=f"skill_id={selected_skill_id} editor_dirty={editor_is_dirty}",
        )
        self.store.add_step(ctx_step, db=db)

        # 3. 准备 studio_agent 参数
        from app.models.workspace import Workspace
        from app.services.llm_gateway import llm_gateway

        ws = db.get(Workspace, conversation.workspace_id) if conversation.workspace_id else None
        workspace_system_context = (ws.system_context or "") if ws else ""
        model_config = llm_gateway.resolve_config(
            db, "conversation.main",
            getattr(ws, "model_config_id", None),
        )

        # 构建历史消息
        history_messages = self._build_history(db, conversation.id, selected_skill_id)

        # 查询可用工具
        available_tools = self._get_available_tools(db)

        # 查询 source_files + memo
        source_files, source_files_content = self._get_source_files(db, selected_skill_id)
        memo_context = self._get_memo(db, selected_skill_id)
        skill_metadata = self._get_skill_metadata(db, selected_skill_id)

        self.store.finish_step(ctx_step, db=db)

        # 4. 记录 model_call step
        model_step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.MODEL_CALL,
            seq=1,
            input_summary=request.input_text[:200],
        )
        self.store.add_step(model_step, db=db)

        # 5. 调用 studio_agent.run_stream — 唯一执行路径
        from app.services.studio_agent import run_stream as studio_run_stream

        full_content = ""
        studio_state_snapshot: dict[str, Any] = {}

        try:
            async for item in studio_run_stream(
                db=db,
                conv_id=conversation.id,
                workspace_system_context=workspace_system_context,
                history_messages=history_messages,
                user_message=request.input_text,
                model_config=model_config,
                selected_skill_id=selected_skill_id,
                editor_prompt=editor_prompt,
                editor_is_dirty=editor_is_dirty,
                available_tools=available_tools,
                source_files=source_files,
                source_files_content=source_files_content,
                memo_context=memo_context,
                skill_metadata=skill_metadata,
            ):
                if isinstance(item, str):
                    # keepalive ping — 透传
                    continue

                evt_name, evt_data = item

                # 捕获 full_content
                if evt_name == "__full_content__":
                    full_content = evt_data.get("text", "")
                    continue

                # 捕获 studio_state_update 用于持久化
                if evt_name == "studio_state_update":
                    studio_state_snapshot = evt_data

                # 映射为 HarnessEvent
                harness_evt = _map_studio_event(
                    evt_name, evt_data,
                    run_id=run.run_id,
                    session_id=harness_session.session_id,
                )
                if harness_evt:
                    yield harness_evt

        except Exception as exc:
            logger.exception("SkillStudioAgentProfile run_stream error for run %s", run.run_id)
            model_step.error = str(exc)
            self.store.finish_step(model_step, db=db)
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=str(exc), db=db)
            yield emit(
                EventName.ERROR,
                {"message": f"Skill Studio 执行失败: {type(exc).__name__}", "error_type": "server_error", "retryable": True},
                run_id=run.run_id,
                session_id=harness_session.session_id,
            )
            return

        # 6. 完成 model step
        model_step.output_summary = full_content[:500] if full_content else ""
        self.store.finish_step(model_step, db=db)

        # 7. 持久化 StudioSessionState
        if studio_state_snapshot:
            _persist_studio_state(self.store, request.session_key, studio_state_snapshot, db=db)

            # 记录 memory ref
            self.store.add_memory_ref(HarnessMemoryRef(
                run_id=run.run_id,
                ref_type="studio_state",
                summary=f"phase={studio_state_snapshot.get('architect_phase', '')} "
                        f"readiness={studio_state_snapshot.get('readiness', 0)}",
                metadata=studio_state_snapshot,
            ), db=db)

        # 8. 完成 run
        self.store.update_run_status(run.run_id, RunStatus.COMPLETED, db=db)
        yield emit(
            EventName.RUN_COMPLETED,
            {"run_id": run.run_id, "full_content_length": len(full_content)},
            run_id=run.run_id,
            session_id=harness_session.session_id,
        )

    async def run_sync(
        self,
        request: HarnessRequest,
        db: Session,
        conversation: Conversation,
        *,
        selected_skill_id: Optional[int] = None,
        editor_prompt: Optional[str] = None,
        editor_is_dirty: bool = False,
    ) -> HarnessResponse:
        """同步执行 — 收集所有事件后返回最终 Response。

        消除同步/流式双轨：同步入口调用同一个 run_stream，只是收集结果。
        """
        full_content = ""
        run_id = ""
        error = None
        final_status = RunStatus.COMPLETED

        async for evt in self.run_stream(
            request, db, conversation,
            selected_skill_id=selected_skill_id,
            editor_prompt=editor_prompt,
            editor_is_dirty=editor_is_dirty,
        ):
            if evt.event == EventName.RUN_STARTED:
                run_id = evt.data.get("run_id", "")
            elif evt.event == EventName.ERROR:
                error = evt.data.get("message", "Unknown error")
                final_status = RunStatus.FAILED
            elif evt.event == EventName.REPLACE:
                full_content = evt.data.get("text", full_content)
            elif evt.event == EventName.DELTA:
                full_content += evt.data.get("text", "")

        return HarnessResponse(
            request_id=request.request_id,
            run_id=run_id,
            status=final_status,
            content=full_content,
            error=error,
        )

    # ── 辅助方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_history(
        db: Session, conv_id: int, skill_id: Optional[int],
        max_rounds: int = 30,
    ) -> list[dict]:
        """构建 LLM 历史消息，按 skill_id 隔离。"""
        if skill_id:
            all_msgs = (
                db.query(Message)
                .filter(Message.conversation_id == conv_id)
                .order_by(Message.created_at)
                .all()
            )
            pairs: list[dict] = []
            pending_user: dict | None = None
            for m in all_msgs:
                if m.role == MessageRole.USER:
                    meta = m.metadata_ or {}
                    if meta.get("skill_id") == skill_id:
                        pending_user = {"role": "user", "content": m.content or ""}
                    else:
                        pending_user = None
                elif m.role == MessageRole.ASSISTANT and pending_user is not None:
                    content = (m.content or "").strip()
                    if content:
                        pairs.append(pending_user)
                        pairs.append({"role": "assistant", "content": content})
                    pending_user = None
            return pairs[-(max_rounds * 2):]
        else:
            all_msgs = (
                db.query(Message)
                .filter(Message.conversation_id == conv_id)
                .order_by(Message.created_at.desc())
                .limit(max_rounds * 2)
                .all()
            )
            return [
                {"role": "user" if m.role == MessageRole.USER else "assistant",
                 "content": (m.content or "").strip() or "(empty)"}
                for m in reversed(all_msgs)
                if (m.content or "").strip()
            ]

    @staticmethod
    def _get_available_tools(db: Session) -> str:
        """查询已发布工具列表。"""
        from app.models.tool import ToolRegistry
        tools = db.query(ToolRegistry).filter(ToolRegistry.status == "published").limit(50).all()
        if tools:
            return "\n".join(
                f"  - [{t.id}] {t.display_name or t.name}（{t.tool_type.value}）：{t.description or '无描述'}"
                for t in tools
            )
        return "（暂无已注册工具）"

    @staticmethod
    def _get_source_files(db: Session, skill_id: Optional[int]) -> tuple[list[dict], str]:
        """获取 skill 附属文件列表和内容。"""
        if not skill_id:
            return [], ""
        from app.models.skill import Skill
        skill = db.get(Skill, skill_id)
        if not skill:
            return [], ""
        source_files = list(skill.source_files or [])
        content = ""
        if source_files:
            from app.services.skill_engine import _read_source_files
            content = _read_source_files(skill_id, source_files)
        return source_files, content

    @staticmethod
    def _get_memo(db: Session, skill_id: Optional[int]) -> Optional[dict]:
        """获取 skill memo 上下文。"""
        if not skill_id:
            return None
        from app.services.skill_memo_service import get_memo
        return get_memo(db, skill_id)

    @staticmethod
    def _get_skill_metadata(db: Session, skill_id: Optional[int]) -> Optional[dict]:
        """获取 skill 元数据。"""
        if not skill_id:
            return None
        from app.models.skill import Skill
        sk = db.get(Skill, skill_id)
        if not sk:
            return None
        return {
            "source_type": getattr(sk, "source_type", None),
            "skill_id": sk.id,
            "name": getattr(sk, "name", None),
        }


# 模块级单例
skill_studio_profile = SkillStudioAgentProfile()
