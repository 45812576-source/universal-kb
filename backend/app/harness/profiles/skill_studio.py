"""SkillStudioAgentProfile — Skill Studio 统一执行 Profile。

G3 核心交付：消除同步/流式双轨，统一走 SkillStudioAgentProfile。
- 同步入口: run_sync() — 内部调 run_stream 收集结果
- 流式入口: run_stream() — 唯一执行主链
- StudioSessionState 持久化到 HarnessSession.metadata

使用方：conversations.py 的 skill_studio 路径（同步 + 流式）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
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
from app.services.studio_context_digest import build_context_digest_bundle
from app.services.studio_latency_policy import build_sla_policy, merge_latency_metadata_fields

logger = logging.getLogger(__name__)

_SOURCE_CONTENT_KEYWORDS = (
    "读取文件", "看文件", "按文件", "当前文件", "这个文件", "附属文件", "源文件",
    "source file", "asset", "SKILL.md", "md 文件", "代码文件", "逐文件", "文件内容",
)
_FIRST_TOKEN_EVENTS = {"delta", "content_block_delta"}
_FIRST_USEFUL_EVENTS = {"replace", "delta", "fallback_text"}
_STREAM_CONTENT_EVENTS = {
    "delta",
    "replace",
    "fallback_text",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _should_load_source_file_content(user_message: str, source_files: list[dict]) -> bool:
    if not source_files:
        return False
    text = (user_message or "").lower()
    return any(keyword.lower() in text for keyword in _SOURCE_CONTENT_KEYWORDS)


def _build_latency_metadata_patch(
    existing_metadata: dict[str, Any] | None,
    **updates: str | None,
) -> dict[str, Any]:
    patch = {key: value for key, value in updates.items() if value}
    merged = merge_latency_metadata_fields(existing_metadata, patch)
    latency = merged.get("latency")
    if latency:
        patch["latency"] = latency
    return patch


def _ordered_sla_checkpoints(policy: dict[str, Any] | None) -> list[tuple[str, float]]:
    if not isinstance(policy, dict) or not policy.get("enabled"):
        return []
    checkpoints: list[tuple[str, float]] = []
    for name in ("probe_after_s", "degrade_after_s", "force_two_stage_after_s", "deadline_after_s"):
        value = policy.get(name)
        if isinstance(value, (int, float)) and value > 0:
            checkpoints.append((name, float(value)))
    checkpoints.sort(key=lambda item: item[1])
    return checkpoints


def _build_sla_fallback_text(
    *,
    session_mode: str,
    complexity_level: str,
    execution_strategy: str,
    user_message: str,
    has_memo: bool,
    source_file_count: int,
) -> str:
    user_summary = " ".join((user_message or "").strip().split())[:80]
    context_bits: list[str] = []
    if has_memo:
        context_bits.append("已加载 memo")
    if source_file_count:
        context_bits.append(f"已识别 {source_file_count} 个源文件索引")
    context_hint = f"（{'，'.join(context_bits)}）" if context_bits else ""

    if session_mode == "create_new_skill":
        focus = "我先交付首轮结构：目标用户/场景、输入输出、约束与验收标准。"
        next_step = "深层补完会继续细化 Skill 边界与提问路径。"
    elif session_mode == "audit_imported_skill":
        focus = "我先交付首轮审计方向：根因清晰度、要素完备性、场景鲁棒性、失败预防。"
        next_step = "深层补完会继续回填治理卡片和整改优先级。"
    else:
        focus = "我先交付首轮优化方向：优先风险、改动落点与建议顺序。"
        next_step = "深层补完会继续展开治理建议和可采纳修改。"

    return (
        f"{focus}{context_hint}\n\n"
        f"当前识别为 `{complexity_level}` 复杂度，执行策略是 `{execution_strategy}`。"
        f"本轮需求摘要：{user_summary or '待根据本轮消息继续收敛'}。\n\n"
        f"{next_step}"
    )


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
                "target_type": session_key.target_type,
                "target_id": session_key.target_id,
                "studio_state": state_dict,
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

    def _update_run_latency(
        self,
        run_id: str,
        *,
        db: Optional[Session] = None,
        **updates: str | None,
    ) -> None:
        run = self.store.get_run(run_id)
        existing_metadata = run.metadata if run else {}
        metadata_patch = _build_latency_metadata_patch(existing_metadata, **updates)
        if metadata_patch:
            self.store.update_run_metadata(run_id, metadata_patch, db=db)

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
        request_accepted_at = _now_iso()
        self.store.update_run_metadata(run.run_id, {"request_accepted_at": request_accepted_at}, db=db)

        yield emit(
            EventName.RUN_STARTED,
            {"run_id": run.run_id, "agent_type": "skill_studio", "request_accepted_at": request_accepted_at},
            run_id=run.run_id,
            session_id=harness_session.session_id,
        )
        yield emit(
            EventName.STATUS,
            {"stage": "accepted", "request_accepted_at": request_accepted_at},
            run_id=run.run_id,
            session_id=harness_session.session_id,
        )

        # 2. 记录 context assembly step
        request_step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.REQUEST_RECEIVED,
            seq=0,
            input_summary=request.input_text[:200],
        )
        self.store.add_step(request_step, db=db)
        self.store.finish_step(request_step, db=db)

        ctx_step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.CONTEXT_ASSEMBLED,
            seq=1,
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
        source_files, source_files_content, source_files_content_loaded = self._get_source_files(
            db,
            selected_skill_id,
            user_message=request.input_text,
        )
        memo_context = self._get_memo(db, selected_skill_id)
        skill_metadata = self._get_skill_metadata(db, selected_skill_id)
        context_digest = build_context_digest_bundle(
            history_messages=history_messages,
            memo_context=memo_context,
            source_files=source_files,
            editor_prompt=editor_prompt,
            persisted_cache=(memo_context or {}).get("context_digest_cache") if isinstance(memo_context, dict) else None,
            include_cache_payload=True,
        )
        if selected_skill_id and context_digest.get("cache", {}).get("cache_changed"):
            from app.services.skill_memo_service import update_context_digest_cache

            updated_memo = update_context_digest_cache(
                db,
                selected_skill_id,
                context_digest.get("cache_payload") or {},
                user_id=conversation.user_id,
                commit=False,
            )
            if isinstance(updated_memo, dict):
                memo_context = updated_memo

        from app.services.studio_latency_policy import (
            choose_execution_strategy,
            estimate_complexity_level,
        )
        from app.services.studio_rollout import (
            apply_rollout_to_execution_strategy,
            lane_statuses_for_rollout,
            resolve_rollout_decision,
        )
        from app.services.studio_router import route_session

        predicted_route = route_session(db, skill_id=selected_skill_id, user_message=request.input_text)
        rollout_decision = resolve_rollout_decision(
            db,
            user_id=conversation.user_id,
            session_mode=predicted_route.session_mode,
            workflow_mode=predicted_route.workflow_mode,
        )
        complexity_level = estimate_complexity_level(
            session_mode=predicted_route.session_mode,
            workflow_mode=predicted_route.workflow_mode,
            next_action=predicted_route.next_action,
            user_message=request.input_text,
            has_files=bool(source_files),
            has_memo=bool(memo_context),
            history_count=len(history_messages),
        )
        execution_strategy = choose_execution_strategy(
            complexity_level=complexity_level,
            workflow_mode=predicted_route.workflow_mode,
            next_action=predicted_route.next_action,
        )
        execution_strategy = apply_rollout_to_execution_strategy(
            execution_strategy,
            flags=rollout_decision.flags,
        )
        lane_statuses = lane_statuses_for_rollout(execution_strategy, flags=rollout_decision.flags)
        sla_policy = build_sla_policy(
            complexity_level=complexity_level,
            execution_strategy=execution_strategy,
            sla_degrade_enabled=rollout_decision.flags.sla_degrade_enabled,
        )
        initial_deep_started_at = ""

        self.store.finish_step(ctx_step, db=db)
        context_ready_at = _now_iso()
        self.store.update_run_metadata(run.run_id, {
            "context_ready_at": context_ready_at,
            "history_count": len(history_messages),
            "source_file_count": len(source_files),
            "source_files_content_loaded": source_files_content_loaded,
            "has_memo": bool(memo_context),
            "context_digest": context_digest,
        }, db=db)
        yield emit(
            EventName.STATUS,
            {
                "stage": "context_ready",
                "context_ready_at": context_ready_at,
                "history_count": len(history_messages),
                "source_file_count": len(source_files),
                "source_files_content_loaded": source_files_content_loaded,
                "has_memo": bool(memo_context),
                "context_digest": context_digest,
            },
            run_id=run.run_id,
            session_id=harness_session.session_id,
        )
        if lane_statuses.get("fast_status") != "not_requested":
            fast_started_at = _now_iso()
            self._update_run_latency(run.run_id, db=db, fast_started_at=fast_started_at)
            yield emit(
                EventName.STATUS,
                {
                    "stage": "fast_started",
                    "fast_started_at": fast_started_at,
                    "complexity_level": complexity_level,
                    "execution_strategy": execution_strategy,
                },
                run_id=run.run_id,
                session_id=harness_session.session_id,
            )
        elif lane_statuses.get("deep_status") != "not_requested":
            deep_started_at = _now_iso()
            initial_deep_started_at = deep_started_at
            self._update_run_latency(run.run_id, db=db, deep_started_at=deep_started_at)
            yield emit(
                EventName.STATUS,
                {
                    "stage": "deep_started",
                    "deep_started_at": deep_started_at,
                    "reason": "fast_lane_not_requested",
                    "execution_strategy": execution_strategy,
                },
                run_id=run.run_id,
                session_id=harness_session.session_id,
            )

        # 4. 记录 model_call step
        model_step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.MODEL_CALL,
            seq=2,
            input_summary=request.input_text[:200],
        )
        self.store.add_step(model_step, db=db)

        # 5. 调用 studio_agent.run_stream — 唯一执行路径
        from app.services.studio_agent import run_stream as studio_run_stream

        full_content = ""
        studio_state_snapshot: dict[str, Any] = {}
        stream_started_at = time.monotonic()

        try:
            first_useful_response_at = ""
            first_token_at = ""
            deep_started_at = initial_deep_started_at
            deep_completed_at = ""
            synthetic_first_response = False
            emitted_checkpoints: set[str] = set()
            queue: asyncio.Queue[Any] = asyncio.Queue()
            producer_failed: BaseException | None = None
            producer_done = object()

            async def _produce_events() -> None:
                nonlocal producer_failed
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
                        await queue.put(item)
                except BaseException as exc:  # noqa: BLE001
                    producer_failed = exc
                finally:
                    await queue.put(producer_done)

            producer_task = asyncio.create_task(_produce_events())

            while True:
                elapsed = time.monotonic() - stream_started_at
                pending_checkpoints = [
                    (name, after_s)
                    for name, after_s in _ordered_sla_checkpoints(sla_policy)
                    if name not in emitted_checkpoints and not first_useful_response_at
                ]
                wait_timeout = None
                if pending_checkpoints:
                    wait_timeout = max(0.0, min(after_s - elapsed for _, after_s in pending_checkpoints))

                try:
                    item = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    checkpoint_name, _ = pending_checkpoints[0]
                    emitted_checkpoints.add(checkpoint_name)
                    checkpoint_at = _now_iso()

                    if checkpoint_name == "probe_after_s":
                        self._update_run_latency(run.run_id, db=db, sla_checkpoint_at=checkpoint_at)
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "sla_checkpoint",
                                "sla_checkpoint_at": checkpoint_at,
                                "checkpoint": "probe",
                                "complexity_level": complexity_level,
                                "execution_strategy": execution_strategy,
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        continue

                    if checkpoint_name == "degrade_after_s":
                        self._update_run_latency(run.run_id, db=db, sla_degraded_at=checkpoint_at)
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "sla_degraded",
                                "sla_degraded_at": checkpoint_at,
                                "degrade_mode": "compact_first_response",
                                "complexity_level": complexity_level,
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        continue

                    if checkpoint_name == "force_two_stage_after_s":
                        if not deep_started_at and lane_statuses.get("deep_status") != "not_requested":
                            deep_started_at = checkpoint_at
                            self._update_run_latency(
                                run.run_id,
                                db=db,
                                deep_started_at=deep_started_at,
                                two_stage_forced_at=checkpoint_at,
                            )
                        else:
                            self._update_run_latency(run.run_id, db=db, two_stage_forced_at=checkpoint_at)
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "two_stage_forced",
                                "two_stage_forced_at": checkpoint_at,
                                "deep_started_at": deep_started_at or checkpoint_at,
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        continue

                    if checkpoint_name == "deadline_after_s":
                        synthetic_first_response = True
                        first_useful_response_at = checkpoint_at
                        self._update_run_latency(
                            run.run_id,
                            db=db,
                            first_useful_response_at=first_useful_response_at,
                            sla_degraded_at=checkpoint_at,
                        )
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "sla_degraded",
                                "sla_degraded_at": checkpoint_at,
                                "degrade_mode": "forced_first_response",
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "first_useful_response",
                                "first_useful_response_at": first_useful_response_at,
                                "source": "sla_fallback",
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        if lane_statuses.get("deep_status") != "not_requested" and not deep_started_at:
                            deep_started_at = checkpoint_at
                            self._update_run_latency(run.run_id, db=db, deep_started_at=deep_started_at)
                            yield emit(
                                EventName.STATUS,
                                {
                                    "stage": "deep_started",
                                    "deep_started_at": deep_started_at,
                                    "reason": "sla_fallback",
                                },
                                run_id=run.run_id,
                                session_id=harness_session.session_id,
                            )
                        yield emit(
                            EventName.FALLBACK_TEXT,
                            {
                                "text": _build_sla_fallback_text(
                                    session_mode=predicted_route.session_mode,
                                    complexity_level=complexity_level,
                                    execution_strategy=execution_strategy,
                                    user_message=request.input_text,
                                    has_memo=bool(memo_context),
                                    source_file_count=len(source_files),
                                ),
                                "source": "sla_fallback",
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )
                        continue

                if item is producer_done:
                    if producer_failed is not None:
                        raise producer_failed
                    break

                if isinstance(item, str):
                    continue

                evt_name, evt_data = item

                if evt_name == "__full_content__":
                    full_content = evt_data.get("text", "")
                    continue

                if evt_name == "status" and evt_data.get("stage") == "classified":
                    complexity_level = str(evt_data.get("complexity_level") or complexity_level)
                    execution_strategy = str(evt_data.get("execution_strategy") or execution_strategy)
                    lane_statuses = {
                        "fast_status": str(evt_data.get("fast_status") or lane_statuses.get("fast_status") or "pending"),
                        "deep_status": str(evt_data.get("deep_status") or lane_statuses.get("deep_status") or "pending"),
                    }
                    sla_policy = build_sla_policy(
                        complexity_level=complexity_level,
                        execution_strategy=execution_strategy,
                        sla_degrade_enabled=rollout_decision.flags.sla_degrade_enabled,
                    )

                if not first_token_at and evt_name in _FIRST_TOKEN_EVENTS:
                    first_token_at = _now_iso()
                    self._update_run_latency(run.run_id, db=db, first_token_at=first_token_at)
                    yield emit(
                        EventName.STATUS,
                        {
                            "stage": "first_token",
                            "first_token_at": first_token_at,
                        },
                        run_id=run.run_id,
                        session_id=harness_session.session_id,
                    )

                if not first_useful_response_at and evt_name in _FIRST_USEFUL_EVENTS:
                    first_useful_response_at = _now_iso()
                    self._update_run_latency(run.run_id, db=db, first_useful_response_at=first_useful_response_at)
                    yield emit(
                        EventName.STATUS,
                        {
                            "stage": "first_useful_response",
                            "first_useful_response_at": first_useful_response_at,
                            "source": "model_stream",
                        },
                        run_id=run.run_id,
                        session_id=harness_session.session_id,
                    )
                    if lane_statuses.get("deep_status") != "not_requested" and not deep_started_at:
                        deep_started_at = _now_iso()
                        self._update_run_latency(run.run_id, db=db, deep_started_at=deep_started_at)
                        yield emit(
                            EventName.STATUS,
                            {
                                "stage": "deep_started",
                                "deep_started_at": deep_started_at,
                                "reason": "first_useful_response_delivered",
                            },
                            run_id=run.run_id,
                            session_id=harness_session.session_id,
                        )

                if evt_name == "status" and evt_data.get("stage") == "done" and deep_started_at and not deep_completed_at:
                    deep_completed_at = _now_iso()
                    self._update_run_latency(run.run_id, db=db, deep_completed_at=deep_completed_at)
                    yield emit(
                        EventName.STATUS,
                        {
                            "stage": "deep_completed",
                            "deep_completed_at": deep_completed_at,
                        },
                        run_id=run.run_id,
                        session_id=harness_session.session_id,
                    )

                if evt_name == "studio_state_update":
                    studio_state_snapshot = evt_data

                if synthetic_first_response and evt_name in _STREAM_CONTENT_EVENTS:
                    continue

                harness_evt = _map_studio_event(
                    evt_name, evt_data,
                    run_id=run.run_id,
                    session_id=harness_session.session_id,
                )
                if harness_evt:
                    yield harness_evt

            if synthetic_first_response and full_content:
                yield emit(
                    EventName.REPLACE,
                    {"text": full_content, "source": "sla_final_replace"},
                    run_id=run.run_id,
                    session_id=harness_session.session_id,
                )

            await producer_task

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
        run_completed_at = _now_iso()
        self._update_run_latency(run.run_id, db=db, run_completed_at=run_completed_at)
        yield emit(
            EventName.RUN_COMPLETED,
            {
                "run_id": run.run_id,
                "full_content_length": len(full_content),
                "run_completed_at": run_completed_at,
            },
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
    def _get_source_files(
        db: Session,
        skill_id: Optional[int],
        *,
        user_message: str = "",
    ) -> tuple[list[dict], str, bool]:
        """获取 skill 附属文件列表和内容。"""
        if not skill_id:
            return [], "", False
        from app.models.skill import Skill
        skill = db.get(Skill, skill_id)
        if not skill:
            return [], "", False
        source_files = list(skill.source_files or [])
        content = ""
        should_load_content = _should_load_source_file_content(user_message, source_files)
        if should_load_content:
            from app.services.skill_engine import _read_source_files
            content = _read_source_files(skill_id, source_files)
        return source_files, content, should_load_content

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
