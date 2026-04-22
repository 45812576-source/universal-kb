"""Skill Studio background runs — hot cache + DB-backed event log.

Phase B1/B2: public_run_id 是前端唯一 run 身份，内层 harness_run_id 只做审计。
StudioRunRegistry 做热缓存（进程内 SSE 流），每个 event append 同步写 DB。
后端重启后可从 DB replay 恢复。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.database import SessionLocal
from app.models.conversation import Conversation, Message, MessageRole
from app.services.studio_patch_bus import attach_run_context, build_patch_envelope, patch_type_for_event
from app.services.studio_rollout import rollout_flag_from_workflow_state
from app.services.studio_workflow_adapter import build_workflow_event_envelope
from app.services import studio_run_event_store

logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass
class StudioRun:
    """热缓存中的 run 对象。id 即 public_run_id。"""
    id: str  # public_run_id
    conversation_id: int
    user_id: int
    skill_id: int | None
    content: str
    req_payload: dict[str, Any] = field(default_factory=dict)
    run_version: int = 1
    harness_run_id: str | None = None
    parent_run_id: str | None = None
    status: str = "queued"
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    updated_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    events: list[tuple[int, str, dict]] = field(default_factory=list)
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    error: str | None = None
    message_id: int | None = None
    patch_seq: int = 0
    superseded_by: str | None = None
    superseded_at: str | None = None
    patch_protocol_enabled: bool = True
    frontend_run_protocol_enabled: bool = True
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    # DB row 是否已创建
    _db_persisted: bool = False

    @property
    def public_run_id(self) -> str:
        return self.id

    def summary(self) -> dict:
        return {
            "id": self.id,
            "public_run_id": self.id,
            "run_id": self.id,
            "run_version": self.run_version,
            "harness_run_id": self.harness_run_id,
            "parent_run_id": self.parent_run_id,
            "conversation_id": self.conversation_id,
            "skill_id": self.skill_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "latest_event_offset": len(self.events),
            "error": self.error,
            "message_id": self.message_id,
            "superseded_by": self.superseded_by,
            "superseded_at": self.superseded_at,
        }


class StudioRunRegistry:
    """进程内热缓存 + DB 持久化。

    - 热缓存服务 SSE 流（低延迟）
    - DB 服务 replay / 恢复 / 审计
    """

    def __init__(self) -> None:
        self._runs: dict[str, StudioRun] = {}
        self._active_by_conversation: dict[int, str] = {}
        self._version_by_conversation: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        conversation_id: int,
        user_id: int,
        skill_id: int | None,
        content: str,
        req_payload: dict[str, Any],
    ) -> StudioRun:
        async with self._lock:
            existing_id = self._active_by_conversation.get(conversation_id)
            run_id = uuid.uuid4().hex
            if existing_id:
                existing = self._runs.get(existing_id)
                if existing and existing.status in {"queued", "running"}:
                    await self._supersede(existing, superseded_by=run_id)
            next_version = self._version_by_conversation.get(conversation_id, 0) + 1
            run = StudioRun(
                id=run_id,
                conversation_id=conversation_id,
                user_id=user_id,
                skill_id=skill_id,
                content=content,
                req_payload=dict(req_payload or {}),
                run_version=next_version,
            )
            self._runs[run.id] = run
            self._active_by_conversation[conversation_id] = run.id
            self._version_by_conversation[conversation_id] = next_version

            # DB 持久化: 创建 agent_runs 记录
            self._persist_run_create(run)

            run.task = asyncio.create_task(self._execute(run, req_payload))
            return run

    async def get_active(self, conversation_id: int, user_id: int) -> StudioRun | None:
        async with self._lock:
            run_id = self._active_by_conversation.get(conversation_id)
            run = self._runs.get(run_id or "")
            if not run or run.user_id != user_id or run.status not in {"queued", "running"}:
                return None
            return run

    async def get(self, run_id: str, user_id: int) -> StudioRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or run.user_id != user_id:
                return None
            return run

    async def cancel(self, run_id: str, user_id: int) -> StudioRun | None:
        """取消 run — Phase B5: 显式停止 harness + pending tool，输出 cancel patches。"""
        run = await self.get(run_id, user_id)
        if not run:
            return None
        run.cancel_requested = True
        if run.task and not run.task.done():
            run.task.cancel()

        # 1. stale_patch — 此 run 所有产出标记 stale
        await self._append(run, "stale_patch", {
            "stale_run_id": run.id,
            "stale_run_version": run.run_version,
            "reason": "user_cancelled",
        })

        # 2. status 事件
        await self._append(run, "status", {"stage": "cancelled"})

        run.status = "cancelled"
        run.updated_at = datetime.datetime.utcnow()

        # DB 持久化
        self._persist_run_status(run, "cancelled")
        return run

    async def stream(self, run: StudioRun, after: int = 0):
        cursor = max(after, 0)
        while True:
            while cursor < len(run.events):
                _, event, data = run.events[cursor]
                cursor += 1
                yield _sse(event, data)
            if run.status in {"completed", "failed", "cancelled", "superseded"}:
                break
            async with run.condition:
                try:
                    await asyncio.wait_for(run.condition.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"

    async def replay_from_db(self, public_run_id: str):
        """从 DB 读取事件流，用于后端重启后恢复。"""
        db = SessionLocal()
        try:
            events = studio_run_event_store.get_all_events(db, public_run_id)
            for evt in events:
                yield _sse(evt.event_type, evt.payload_json or {})
        finally:
            db.close()

    async def _append(self, run: StudioRun, event: str, data: dict) -> None:
        payload = attach_run_context(
            data,
            run_id=run.id,
            run_version=run.run_version,
            workflow_id=run.id,
        )
        async with run.condition:
            seq = len(run.events) + 1
            run.events.append((seq, event, payload))

            # DB 持久化: 写入 event
            self._persist_event(run, seq, event, payload)

            if run.patch_protocol_enabled:
                patch = self._build_patch_event(run, event, payload)
                if patch is not None:
                    patch_seq = len(run.events) + 1
                    run.events.append((patch_seq, "patch_applied", patch))
                    self._persist_event(run, patch_seq, "patch_applied", patch, patch_type=str(patch.get("patch_type", "")))

            if event != "workflow_event" and run.frontend_run_protocol_enabled:
                envelope = self._build_workflow_event(run, event, payload)
                if envelope is not None:
                    wf_seq = len(run.events) + 1
                    run.events.append((wf_seq, "workflow_event", envelope))
                    self._persist_event(run, wf_seq, "workflow_event", envelope)

            run.updated_at = datetime.datetime.utcnow()
            run.condition.notify_all()

    def _build_patch_event(self, run: StudioRun, event: str, data: dict) -> dict | None:
        patch_type = patch_type_for_event(event)
        if not patch_type:
            return None
        run.patch_seq += 1
        return build_patch_envelope(
            run_id=run.id,
            run_version=run.run_version,
            patch_seq=run.patch_seq,
            patch_type=patch_type,
            payload=data,
        )

    def _build_workflow_event(self, run: StudioRun, event: str, data: dict) -> dict | None:
        if event == "workflow_state":
            return build_workflow_event_envelope(
                event_type="state_changed",
                payload=data,
                source_type="workflow",
                phase=str(data.get("phase") or "discover"),
                workflow_id=run.id,
                skill_id=run.skill_id,
                conversation_id=run.conversation_id,
                step=event,
            )
        mapping = {
            "route_status": ("route_status_changed", "router", "discover"),
            "assist_skills_status": ("assist_skills_changed", "router", "discover"),
            "governance_card": ("card_created", "governance", "review"),
            "staged_edit_notice": ("staged_edit_created", "governance", "review"),
            "audit_summary": ("audit_completed", "audit", "review"),
            "architect_phase_status": ("phase_changed", "architect", "design"),
            "status": ("status_changed", "workflow", "discover"),
        }
        target = mapping.get(event)
        if not target:
            return None
        event_type, source_type, phase = target
        return build_workflow_event_envelope(
            event_type=event_type,
            payload=data,
            source_type=source_type,
            phase=phase,
            workflow_id=run.id,
            skill_id=run.skill_id,
            conversation_id=run.conversation_id,
            step=event,
        )

    @staticmethod
    def _drop_event_type(run: StudioRun, event_name: str) -> None:
        run.events = [
            (index + 1, current_event, payload)
            for index, (_, current_event, payload) in enumerate(run.events)
            if current_event != event_name
        ]

    async def _supersede(self, run: StudioRun, *, superseded_by: str) -> None:
        """标记旧 run 为 superseded，发出 stale 信号防止污染新 run。

        Phase B5: 旧 run 的所有 pending card/artifact/staged_edit 标记 stale。
        """
        run.cancel_requested = True
        run.status = "superseded"
        run.superseded_by = superseded_by
        run.superseded_at = datetime.datetime.utcnow().isoformat()
        if run.task and not run.task.done():
            run.task.cancel()

        # 1. run_superseded 事件
        await self._append(run, "run_superseded", {
            **run.summary(),
            "status": "superseded",
            "superseded_by": superseded_by,
        })

        # 2. stale_patch — 告知前端此 run 的所有产出均为 stale
        await self._append(run, "stale_patch", {
            "stale_run_id": run.id,
            "stale_run_version": run.run_version,
            "superseded_by": superseded_by,
            "reason": "new_message_superseded",
        })

        # 3. status 事件
        await self._append(run, "status", {"stage": "superseded", "superseded_by": superseded_by})

        # DB 持久化
        self._persist_run_status(run, "superseded", superseded_by=superseded_by)

    async def _emit_deep_lane_patches(
        self,
        run: StudioRun,
        *,
        final_content: str,
        deep_lane_expected: bool,
        first_useful_response_seen: bool,
        deep_started_seen: bool,
        deep_completed_seen: bool,
        audit_summary: dict[str, Any] | None,
        governance_card_count: int,
        staged_edit_count: int,
    ) -> None:
        if not run.patch_protocol_enabled:
            return
        if not deep_lane_expected:
            return
        if not first_useful_response_seen:
            return
        if not (deep_started_seen or deep_completed_seen or audit_summary or governance_card_count or staged_edit_count):
            return

        summary = (final_content or "").strip()
        if summary:
            await self._append(run, "deep_summary", {
                "title": "审计补完" if audit_summary else "Deep Lane 补完",
                "summary": summary,
                "text": summary,
                "status": "completed" if deep_completed_seen else "running",
            })

        evidence: list[str] = []
        if audit_summary:
            evidence.append("已产出审计结论")
        if governance_card_count:
            evidence.append(f"已生成 {governance_card_count} 张治理卡片")
        if staged_edit_count:
            evidence.append(f"已生成 {staged_edit_count} 个 staged edit")
        if deep_completed_seen:
            evidence.append("Deep Lane 已完成")
        elif deep_started_seen:
            evidence.append("Deep Lane 已启动")

        if evidence:
            await self._append(run, "deep_evidence", {
                "title": "证据补充",
                "summary": "Deep Lane 已生成补完证据",
                "evidence": evidence,
            })

    def metrics_snapshot(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        patch_counts: dict[str, int] = {}
        active_runs = 0
        superseded_runs = 0

        for run in self._runs.values():
            status_counts[run.status] = status_counts.get(run.status, 0) + 1
            if run.status in {"queued", "running"}:
                active_runs += 1
            if run.status == "superseded":
                superseded_runs += 1
            for _, event_name, payload in run.events:
                if event_name != "patch_applied" or not isinstance(payload, dict):
                    continue
                patch_type = str(payload.get("patch_type") or "")
                if patch_type:
                    patch_counts[patch_type] = patch_counts.get(patch_type, 0) + 1

        return {
            "total_runs": len(self._runs),
            "active_runs": active_runs,
            "superseded_runs": superseded_runs,
            "status_counts": status_counts,
            "patch_counts": patch_counts,
        }

    async def _execute(self, run: StudioRun, req_payload: dict[str, Any]) -> None:
        db = SessionLocal()
        run.status = "running"
        self._persist_run_status(run, "running")
        await self._append(run, "studio_run", run.summary())
        final_content = ""
        deep_lane_expected = False
        first_useful_response_seen = False
        deep_started_seen = False
        deep_completed_seen = False
        audit_summary: dict[str, Any] | None = None
        governance_card_count = 0
        staged_edit_count = 0
        try:
            conv = db.get(Conversation, run.conversation_id)
            if not conv:
                raise RuntimeError("Conversation not found")

            await self._append(run, "status", {"stage": "preparing"})

            from app.harness.adapters import build_skill_studio_request
            from app.config import settings as app_settings

            studio_req = build_skill_studio_request(
                user_id=run.user_id,
                workspace_id=conv.workspace_id or 0,
                skill_id=run.skill_id or conv.skill_id or 0,
                conversation_id=run.conversation_id,
                user_message=run.content,
                stream=True,
                metadata={
                    "source": "studio_runs",
                    "public_run_id": run.id,
                    "run_version": run.run_version,
                    **{k: v for k, v in req_payload.items() if v is not None},
                },
            )

            if app_settings.STUDIO_STRUCTURED_MODE == "on":
                from app.services.studio_workflow_orchestrator import bootstrap_workflow

                try:
                    bootstrap = await bootstrap_workflow(
                        db,
                        workflow_id=run.id,
                        conversation_id=run.conversation_id,
                        skill_id=run.skill_id,
                        user_message=run.content,
                        user_id=run.user_id,
                    )
                    run.patch_protocol_enabled = rollout_flag_from_workflow_state(
                        bootstrap.workflow_state,
                        flag_key="patch_protocol_enabled",
                        default=True,
                    )
                    run.frontend_run_protocol_enabled = rollout_flag_from_workflow_state(
                        bootstrap.workflow_state,
                        flag_key="frontend_run_protocol_enabled",
                        default=True,
                    )
                    if not run.patch_protocol_enabled:
                        self._drop_event_type(run, "patch_applied")
                    if not run.frontend_run_protocol_enabled:
                        self._drop_event_type(run, "workflow_event")
                    await self._append(run, "workflow_state", bootstrap.workflow_state)
                    deep_lane_expected = (
                        str(bootstrap.workflow_state.get("execution_strategy") or "") != "fast_only"
                        and str(bootstrap.workflow_state.get("deep_status") or "") != "not_requested"
                    )
                    await self._append(run, "route_status", bootstrap.route_status)
                    await self._append(run, "assist_skills_status", bootstrap.assist_skills_status)
                    if bootstrap.architect_phase_status:
                        await self._append(run, "architect_phase_status", bootstrap.architect_phase_status)
                    if bootstrap.audit_summary:
                        audit_summary = dict(bootstrap.audit_summary)
                        await self._append(run, "audit_summary", bootstrap.audit_summary)
                    for card in bootstrap.cards:
                        governance_card_count += 1
                        await self._append(run, "governance_card", card)
                    for staged_edit in bootstrap.staged_edits:
                        staged_edit_count += 1
                        await self._append(run, "staged_edit_notice", staged_edit)
                except Exception as bootstrap_err:
                    logger.warning("[studio_run] workflow bootstrap failed: %s", bootstrap_err)
                    await self._append(run, "fallback_text", {"text": f"工作流初始化失败: {bootstrap_err}"})

            # Phase 8: 灰度分支 — gateway 主链 vs legacy 直调
            from app.harness.gateway import is_gateway_main_chain
            _use_gateway = is_gateway_main_chain()

            if _use_gateway:
                # Gateway 主链：dispatch 负责 executor 调用 + event DB 写入
                from app.harness.gateway import create_gateway
                gateway = create_gateway()
                async for harness_evt in gateway.dispatch(studio_req, db):
                    if run.cancel_requested:
                        raise asyncio.CancelledError()
                    event_name = harness_evt.event.value
                    data = dict(harness_evt.data or {})
                    # 跳过 gateway lifecycle 事件（run_created/run_started/run_completed）
                    # 这些由 StudioRunRegistry 自己管理
                    if event_name in {"run_created", "run_started", "run_completed", "run_failed"}:
                        if event_name == "run_started":
                            run.harness_run_id = data.get("run_id")
                        continue
                    await self._append(run, event_name, data)
                    if event_name == "status":
                        stage = str(data.get("stage") or "")
                        if stage == "first_useful_response":
                            first_useful_response_seen = True
                        elif stage in {"deep_started", "two_stage_forced"}:
                            deep_started_seen = True
                        elif stage == "deep_completed":
                            deep_completed_seen = True
                    elif event_name == "audit_summary":
                        audit_summary = data
                    elif event_name == "governance_card":
                        governance_card_count += 1
                    elif event_name == "staged_edit_notice":
                        staged_edit_count += 1
                    if event_name == "replace":
                        final_content = data.get("text", final_content)
                    elif event_name == "delta":
                        final_content += data.get("text", "")
            else:
                # Legacy 直调：SkillStudioAgentProfile.run_stream()
                from app.harness.profiles.skill_studio import skill_studio_profile
                async for harness_evt in skill_studio_profile.run_stream(
                    studio_req,
                    db,
                    conv,
                    selected_skill_id=run.skill_id,
                    editor_prompt=req_payload.get("editor_prompt"),
                    editor_is_dirty=bool(req_payload.get("editor_is_dirty")),
                    selected_source_filename=req_payload.get("selected_source_filename"),
                    active_card_id=req_payload.get("active_card_id"),
                    active_card_title=req_payload.get("active_card_title"),
                    active_card_mode=req_payload.get("active_card_mode"),
                    active_card_target=req_payload.get("active_card_target"),
                    active_card_source_card_id=req_payload.get("active_card_source_card_id"),
                    active_card_staged_edit_id=req_payload.get("active_card_staged_edit_id"),
                    active_card_phase=req_payload.get("active_card_phase"),
                    active_card_validation_source=req_payload.get("active_card_validation_source"),
                    active_card_file_role=req_payload.get("active_card_file_role"),
                    active_card_handoff_policy=req_payload.get("active_card_handoff_policy"),
                    active_card_route_kind=req_payload.get("active_card_route_kind"),
                    active_card_destination=req_payload.get("active_card_destination"),
                    active_card_return_to=req_payload.get("active_card_return_to"),
                    active_card_queue_window=req_payload.get("active_card_queue_window"),
                    active_card_context_summary=req_payload.get("active_card_context_summary"),
                    active_card_contract_id=req_payload.get("active_card_contract_id"),
                ):
                    if run.cancel_requested:
                        raise asyncio.CancelledError()
                    event_name = harness_evt.event.value
                    data = dict(harness_evt.data or {})
                    # Legacy 路径也跳过 profile 内部 lifecycle 事件，
                    # 避免内部 harness_run_id 泄漏到 replay DB
                    if event_name in {"run_created", "run_started", "run_completed", "run_failed"}:
                        if event_name == "run_started":
                            run.harness_run_id = data.get("run_id")
                        continue
                    await self._append(run, event_name, data)
                    if event_name == "status":
                        stage = str(data.get("stage") or "")
                        if stage == "first_useful_response":
                            first_useful_response_seen = True
                        elif stage in {"deep_started", "two_stage_forced"}:
                            deep_started_seen = True
                        elif stage == "deep_completed":
                            deep_completed_seen = True
                    elif event_name == "audit_summary":
                        audit_summary = data
                    elif event_name == "governance_card":
                        governance_card_count += 1
                    elif event_name == "staged_edit_notice":
                        staged_edit_count += 1
                    if event_name == "replace":
                        final_content = data.get("text", final_content)
                    elif event_name == "delta":
                        final_content += data.get("text", "")

            await self._emit_deep_lane_patches(
                run,
                final_content=final_content,
                deep_lane_expected=deep_lane_expected,
                first_useful_response_seen=first_useful_response_seen,
                deep_started_seen=deep_started_seen,
                deep_completed_seen=deep_completed_seen,
                audit_summary=audit_summary,
                governance_card_count=governance_card_count,
                staged_edit_count=staged_edit_count,
            )

            _card_meta = {
                key: value
                for key, value in (run.req_payload or {}).items()
                if key.startswith("active_card_") and value is not None
            }
            if run.req_payload.get("selected_source_filename") is not None:
                _card_meta["selected_source_filename"] = run.req_payload["selected_source_filename"]
            if "editor_is_dirty" in (run.req_payload or {}):
                _card_meta["editor_is_dirty"] = bool(run.req_payload.get("editor_is_dirty"))
            if run.req_payload.get("editor_prompt") is not None:
                _card_meta["editor_target"] = True
            assistant_msg = Message(
                conversation_id=run.conversation_id,
                role=MessageRole.ASSISTANT,
                content=final_content,
                metadata_={**({"skill_id": run.skill_id} if run.skill_id else {}), "studio_scope": "skill_studio", **_card_meta},
            )
            db.add(assistant_msg)
            msg_count = db.query(Message).filter(Message.conversation_id == run.conversation_id).count()
            if msg_count <= 2:
                conv.title = run.content[:60]
            db.commit()
            run.message_id = assistant_msg.id
            run.status = "completed"
            self._persist_run_status(run, "completed", message_id=assistant_msg.id)
            await self._append(run, "done", {"message_id": assistant_msg.id, "metadata": {}})
        except asyncio.CancelledError:
            db.rollback()
            if run.status == "superseded":
                await self._append(run, "done", {"superseded": True, "superseded_by": run.superseded_by})
            else:
                run.status = "cancelled"
                self._persist_run_status(run, "cancelled")
                await self._append(run, "done", {"cancelled": True})
        except Exception as exc:
            db.rollback()
            run.status = "failed"
            run.error = str(exc) or type(exc).__name__
            logger.exception("[studio_run] run failed")
            self._persist_run_status(run, "failed", error_message=run.error)
            await self._append(run, "error", {
                "message": run.error,
                "error_type": "server_error",
                "retryable": False,
            })
        finally:
            try:
                db.execute(text("SELECT 1"))
            except Exception:
                pass
            db.close()
            async with self._lock:
                if (
                    self._active_by_conversation.get(run.conversation_id) == run.id
                    and run.status in {"completed", "failed", "cancelled", "superseded"}
                ):
                    self._active_by_conversation.pop(run.conversation_id, None)

    # ── DB Persistence Helpers ────────────────────────────────────────────────

    @staticmethod
    def _persist_run_create(run: StudioRun) -> None:
        """创建 agent_runs DB 记录。独立 session 避免与执行 session 冲突。"""
        db = SessionLocal()
        try:
            studio_run_event_store.create_run(
                db,
                public_run_id=run.id,
                conversation_id=run.conversation_id,
                user_id=run.user_id,
                skill_id=run.skill_id,
                run_version=run.run_version,
                parent_run_id=run.parent_run_id,
            )
            db.commit()
            run._db_persisted = True
        except Exception:
            db.rollback()
            logger.exception("[studio_run] DB persist run create failed for %s", run.id)
        finally:
            db.close()

    @staticmethod
    def _persist_run_status(
        run: StudioRun,
        status: str,
        *,
        superseded_by: str | None = None,
        message_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新 agent_runs DB 状态。"""
        db = SessionLocal()
        try:
            studio_run_event_store.update_run_status(
                db,
                run.id,
                status,
                superseded_by=superseded_by,
                message_id=message_id,
                error_message=error_message,
                harness_run_id=run.harness_run_id,
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[studio_run] DB persist status update failed for %s -> %s", run.id, status)
        finally:
            db.close()

    @staticmethod
    def _persist_event(
        run: StudioRun,
        sequence: int,
        event_type: str,
        payload: dict,
        *,
        patch_type: str | None = None,
    ) -> None:
        """追加 event 到 agent_run_events。使用独立 session，失败不影响热缓存。"""
        db = SessionLocal()
        try:
            studio_run_event_store.append_event(
                db,
                public_run_id=run.id,
                run_version=run.run_version,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                patch_type=patch_type,
                harness_run_id=run.harness_run_id,
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.debug("[studio_run] DB event append failed for %s seq=%d", run.id, sequence)
        finally:
            db.close()


studio_run_registry = StudioRunRegistry()
