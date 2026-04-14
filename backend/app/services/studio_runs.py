"""In-memory Skill Studio background runs.

This keeps Studio Chat execution alive when the browser route changes. It is
intentionally lightweight: runs survive client disconnects within the current
backend process and expose replayable SSE events for reconnecting clients.
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

logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass
class StudioRun:
    id: str
    conversation_id: int
    user_id: int
    skill_id: int | None
    content: str
    status: str = "queued"
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    updated_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    events: list[tuple[int, str, dict]] = field(default_factory=list)
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    error: str | None = None
    message_id: int | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "skill_id": self.skill_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "latest_event_offset": len(self.events),
            "error": self.error,
            "message_id": self.message_id,
        }


class StudioRunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, StudioRun] = {}
        self._active_by_conversation: dict[int, str] = {}
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
            if existing_id:
                existing = self._runs.get(existing_id)
                if existing and existing.status in {"queued", "running"}:
                    return existing
            run = StudioRun(
                id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                user_id=user_id,
                skill_id=skill_id,
                content=content,
            )
            self._runs[run.id] = run
            self._active_by_conversation[conversation_id] = run.id
            run.task = asyncio.create_task(self._execute(run, req_payload))
            return run

    async def get_active(self, conversation_id: int, user_id: int) -> StudioRun | None:
        async with self._lock:
            run_id = self._active_by_conversation.get(conversation_id)
            run = self._runs.get(run_id or "")
            if not run or run.user_id != user_id:
                return None
            return run

    async def get(self, run_id: str, user_id: int) -> StudioRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or run.user_id != user_id:
                return None
            return run

    async def cancel(self, run_id: str, user_id: int) -> StudioRun | None:
        run = await self.get(run_id, user_id)
        if not run:
            return None
        run.cancel_requested = True
        if run.task and not run.task.done():
            run.task.cancel()
        await self._append(run, "status", {"stage": "cancelled"})
        run.status = "cancelled"
        run.updated_at = datetime.datetime.utcnow()
        return run

    async def stream(self, run: StudioRun, after: int = 0):
        cursor = max(after, 0)
        while True:
            while cursor < len(run.events):
                _, event, data = run.events[cursor]
                cursor += 1
                yield _sse(event, data)
            if run.status in {"completed", "failed", "cancelled"}:
                break
            async with run.condition:
                try:
                    await asyncio.wait_for(run.condition.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"

    async def _append(self, run: StudioRun, event: str, data: dict) -> None:
        async with run.condition:
            run.events.append((len(run.events) + 1, event, data))
            run.updated_at = datetime.datetime.utcnow()
            run.condition.notify_all()

    async def _execute(self, run: StudioRun, req_payload: dict[str, Any]) -> None:
        db = SessionLocal()
        run.status = "running"
        await self._append(run, "studio_run", run.summary())
        final_content = ""
        try:
            conv = db.get(Conversation, run.conversation_id)
            if not conv:
                raise RuntimeError("Conversation not found")

            await self._append(run, "status", {"stage": "preparing"})

            from app.harness.adapters import build_skill_studio_request
            from app.harness.profiles.skill_studio import skill_studio_profile
            from app.config import settings as app_settings

            studio_req = build_skill_studio_request(
                user_id=run.user_id,
                workspace_id=conv.workspace_id or 0,
                skill_id=run.skill_id or conv.skill_id or 0,
                conversation_id=run.conversation_id,
                user_message=run.content,
                stream=True,
                metadata={"source": "studio_runs"},
            )

            if app_settings.STUDIO_STRUCTURED_MODE == "on":
                msg_count = db.query(Message).filter(Message.conversation_id == run.conversation_id).count()
                if msg_count <= 2:
                    from app.services.studio_router import route_session

                    route_result = route_session(db, skill_id=run.skill_id, user_message=run.content)
                    await self._append(run, "route_status", {
                        "session_mode": route_result.session_mode,
                        "active_assist_skills": route_result.active_assist_skills,
                        "route_reason": route_result.route_reason,
                        "next_action": route_result.next_action,
                        "workflow_mode": route_result.workflow_mode,
                        "initial_phase": route_result.initial_phase,
                    })
                    await self._append(run, "assist_skills_status", {
                        "skills": route_result.active_assist_skills,
                        "session_mode": route_result.session_mode,
                    })

                    if route_result.workflow_mode == "architect_mode":
                        from app.models.skill import ArchitectWorkflowState

                        arch_state = db.query(ArchitectWorkflowState).filter(
                            ArchitectWorkflowState.conversation_id == run.conversation_id
                        ).first()
                        if not arch_state:
                            arch_state = ArchitectWorkflowState(
                                conversation_id=run.conversation_id,
                                skill_id=run.skill_id,
                                workflow_mode="architect_mode",
                                workflow_phase=route_result.initial_phase,
                            )
                            db.add(arch_state)
                            db.commit()
                            db.refresh(arch_state)
                        await self._append(run, "architect_phase_status", {
                            "phase": arch_state.workflow_phase,
                            "mode_source": route_result.session_mode,
                            "ooda_round": arch_state.ooda_round,
                        })

                    if route_result.next_action == "run_audit" and run.skill_id:
                        try:
                            from app.services.studio_auditor import run_audit
                            from app.services.studio_governance import generate_governance_actions

                            audit_result = await run_audit(db, run.skill_id)
                            await self._append(run, "audit_summary", {
                                "verdict": audit_result.verdict,
                                "issues": audit_result.issues,
                                "recommended_path": audit_result.recommended_path,
                                "audit_id": getattr(audit_result, "audit_id", None),
                            })
                            if audit_result.verdict in ("needs_work", "poor"):
                                gov_result = await generate_governance_actions(
                                    db, run.skill_id, audit_id=getattr(audit_result, "audit_id", None)
                                )
                                for card in gov_result.cards:
                                    await self._append(run, "governance_card", card)
                                for staged_edit in gov_result.staged_edits:
                                    await self._append(run, "staged_edit_notice", staged_edit)
                        except Exception as audit_err:
                            logger.warning("[studio_run] auto governance failed: %s", audit_err)
                            await self._append(run, "fallback_text", {"text": f"治理建议生成失败: {audit_err}"})

            async for harness_evt in skill_studio_profile.run_stream(
                studio_req,
                db,
                conv,
                selected_skill_id=run.skill_id,
                editor_prompt=req_payload.get("editor_prompt"),
                editor_is_dirty=bool(req_payload.get("editor_is_dirty")),
            ):
                if run.cancel_requested:
                    raise asyncio.CancelledError()
                event_name = harness_evt.event.value
                data = dict(harness_evt.data or {})
                await self._append(run, event_name, data)
                if event_name == "replace":
                    final_content = data.get("text", final_content)
                elif event_name == "delta":
                    final_content += data.get("text", "")

            assistant_msg = Message(
                conversation_id=run.conversation_id,
                role=MessageRole.ASSISTANT,
                content=final_content,
                metadata_={"skill_id": run.skill_id, "studio_scope": "skill_studio"} if run.skill_id else {"studio_scope": "skill_studio"},
            )
            db.add(assistant_msg)
            msg_count = db.query(Message).filter(Message.conversation_id == run.conversation_id).count()
            if msg_count <= 2:
                conv.title = run.content[:60]
            db.commit()
            run.message_id = assistant_msg.id
            run.status = "completed"
            await self._append(run, "done", {"message_id": assistant_msg.id, "metadata": {}})
        except asyncio.CancelledError:
            db.rollback()
            run.status = "cancelled"
            await self._append(run, "done", {"cancelled": True})
        except Exception as exc:
            db.rollback()
            run.status = "failed"
            run.error = str(exc) or type(exc).__name__
            logger.exception("[studio_run] run failed")
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
                if self._active_by_conversation.get(run.conversation_id) == run.id:
                    self._active_by_conversation.pop(run.conversation_id, None)


studio_run_registry = StudioRunRegistry()
