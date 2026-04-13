"""Hermes Harness SessionStore — 统一 run/step/artifact/approval/memory 读写。

Phase 1 采用 adapter 模式：将 Harness 运行对象映射到现有表（Conversation.metadata、
Message.metadata、UnifiedEvent）。中期应迁移到独立 Harness 状态表。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from app.harness.contracts import (
    AgentType,
    ApprovalStatus,
    HarnessApproval,
    HarnessArtifact,
    HarnessMemoryRef,
    HarnessRun,
    HarnessSession,
    HarnessSessionKey,
    HarnessStep,
    RunStatus,
    StepType,
)
from app.models.event_bus import UnifiedEvent

logger = logging.getLogger(__name__)


class SessionStore:
    """统一状态读写接口。

    Phase 1: 内存 + UnifiedEvent 表 双写。
    - 内存 dict 提供快速查询（进程内）。
    - UnifiedEvent 提供持久化和跨进程 replay。
    """

    def __init__(self) -> None:
        # compound_key -> HarnessSession
        self._sessions: dict[str, HarnessSession] = {}
        # run_id -> HarnessRun
        self._runs: dict[str, HarnessRun] = {}
        # run_id -> [HarnessStep]
        self._steps: dict[str, list[HarnessStep]] = {}
        # run_id -> [HarnessArtifact]
        self._artifacts: dict[str, list[HarnessArtifact]] = {}
        # run_id -> [HarnessApproval]
        self._approvals: dict[str, list[HarnessApproval]] = {}
        # run_id -> [HarnessMemoryRef]
        self._memory_refs: dict[str, list[HarnessMemoryRef]] = {}

    # ── Run ──────────────────────────────────────────────────────────────────

    def create_or_get_session(
        self,
        session_key: HarnessSessionKey,
        *,
        agent_type: Optional[AgentType] = None,
        db: Optional[DBSession] = None,
    ) -> HarnessSession:
        compound = session_key.compound_key
        existing = self._sessions.get(compound)
        if existing:
            existing.updated_at = time.time()
            return existing
        session = HarnessSession(session_key=session_key, agent_type=agent_type or session_key.agent_type)
        self._sessions[compound] = session
        if db:
            self._persist_event(db, "harness.session.created", "", session_key, {
                "session_id": session.session_id,
                "compound_key": compound,
                "agent_type": (agent_type or session_key.agent_type).value,
            })
        return session

    def get_session(self, session_key: HarnessSessionKey) -> Optional[HarnessSession]:
        return self._sessions.get(session_key.compound_key)

    def create_run(self, run: HarnessRun, db: Optional[DBSession] = None) -> HarnessRun:
        self._runs[run.run_id] = run
        if db:
            self._persist_event(db, "harness.run.created", run.run_id, run.session_key, {
                "run_id": run.run_id,
                "request_id": run.request_id,
                "agent_type": run.agent_type.value if run.agent_type else None,
                "status": run.status.value,
            })
        return run

    def update_run_status(self, run_id: str, status: RunStatus, *,
                          error: Optional[str] = None,
                          db: Optional[DBSession] = None) -> Optional[HarnessRun]:
        run = self._runs.get(run_id)
        if not run:
            logger.warning("update_run_status: run %s not found", run_id)
            return None
        run.status = status
        if error:
            run.error = error
        if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            run.finished_at = time.time()
        if db:
            self._persist_event(db, "harness.run.status_changed", run_id, run.session_key, {
                "run_id": run_id,
                "status": status.value,
                "error": error,
            })
        return run

    def get_run(self, run_id: str) -> Optional[HarnessRun]:
        return self._runs.get(run_id)

    # ── Step ─────────────────────────────────────────────────────────────────

    def add_step(self, step: HarnessStep, db: Optional[DBSession] = None) -> HarnessStep:
        self._steps.setdefault(step.run_id, []).append(step)
        if db:
            self._persist_event(db, "harness.step.added", step.run_id, None, {
                "step_id": step.step_id,
                "run_id": step.run_id,
                "step_type": step.step_type.value,
                "seq": step.seq,
            })
        return step

    def finish_step(self, step: HarnessStep, db: Optional[DBSession] = None) -> None:
        step.finished_at = time.time()
        if db:
            self._persist_event(db, "harness.step.finished", step.run_id, None, {
                "step_id": step.step_id,
                "run_id": step.run_id,
                "output_summary": step.output_summary[:500] if step.output_summary else "",
                "error": step.error,
            })

    def get_steps(self, run_id: str) -> list[HarnessStep]:
        return self._steps.get(run_id, [])

    # ── Artifact ─────────────────────────────────────────────────────────────

    def add_artifact(self, artifact: HarnessArtifact, db: Optional[DBSession] = None) -> HarnessArtifact:
        self._artifacts.setdefault(artifact.run_id, []).append(artifact)
        if db:
            self._persist_event(db, "harness.artifact.added", artifact.run_id, None, {
                "artifact_id": artifact.artifact_id,
                "run_id": artifact.run_id,
                "artifact_type": artifact.artifact_type.value,
                "name": artifact.name,
            })
        return artifact

    def get_artifacts(self, run_id: str) -> list[HarnessArtifact]:
        return self._artifacts.get(run_id, [])

    # ── Approval ─────────────────────────────────────────────────────────────

    def add_approval(self, approval: HarnessApproval, db: Optional[DBSession] = None) -> HarnessApproval:
        self._approvals.setdefault(approval.run_id, []).append(approval)
        if db:
            self._persist_event(db, "harness.approval.requested", approval.run_id, None, {
                "approval_id": approval.approval_id,
                "run_id": approval.run_id,
                "step_id": approval.step_id,
                "reason": approval.reason,
            })
        return approval

    def decide_approval(self, approval: HarnessApproval, status: ApprovalStatus, *,
                        decided_by: Optional[int] = None,
                        db: Optional[DBSession] = None) -> None:
        approval.status = status
        approval.decided_at = time.time()
        approval.decided_by = decided_by
        if db:
            self._persist_event(db, "harness.approval.decided", approval.run_id, None, {
                "approval_id": approval.approval_id,
                "status": status.value,
                "decided_by": decided_by,
            })

    def get_approvals(self, run_id: str) -> list[HarnessApproval]:
        return self._approvals.get(run_id, [])

    # ── MemoryRef ────────────────────────────────────────────────────────────

    def add_memory_ref(self, ref: HarnessMemoryRef, db: Optional[DBSession] = None) -> HarnessMemoryRef:
        self._memory_refs.setdefault(ref.run_id, []).append(ref)
        if db:
            self._persist_event(db, "harness.memory_ref.added", ref.run_id, None, {
                "ref_id": ref.ref_id,
                "run_id": ref.run_id,
                "ref_type": ref.ref_type,
                "ref_source_id": ref.ref_source_id,
                "summary": ref.summary,
                "metadata": ref.metadata,
            })
        return ref

    def get_memory_refs(self, run_id: str) -> list[HarnessMemoryRef]:
        return self._memory_refs.get(run_id, [])

    # ── Replay: 按 run_id 获取完整运行快照 ─────────────────────────────────

    def get_run_snapshot(self, run_id: str) -> dict:
        """返回一次运行的完整快照 — 用于 replay 和审计。"""
        run = self.get_run(run_id)
        if not run:
            return {}
        return {
            "session": self._sessions.get(run.session_key.compound_key) if run.session_key else None,
            "run": run,
            "steps": self.get_steps(run_id),
            "artifacts": self.get_artifacts(run_id),
            "approvals": self.get_approvals(run_id),
            "memory_refs": self.get_memory_refs(run_id),
        }

    # ── G5: 查询接口 ─────────────────────────────────────────────────────

    def list_runs(
        self,
        *,
        project_id: int | None = None,
        user_id: int | None = None,
        agent_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HarnessRun]:
        """按条件过滤 runs — 内存查询。"""
        results = []
        for run in self._runs.values():
            if project_id is not None:
                if not (run.session_key and run.session_key.project_id == project_id):
                    continue
            if user_id is not None:
                if not (run.session_key and run.session_key.user_id == user_id):
                    continue
            if agent_type is not None:
                if not (run.agent_type and run.agent_type.value == agent_type):
                    continue
            if status is not None:
                if run.status.value != status:
                    continue
            results.append(run)
        results.sort(key=lambda r: r.started_at, reverse=True)
        return results[:limit]

    def list_artifacts(
        self,
        *,
        project_id: int | None = None,
        artifact_type: str | None = None,
        limit: int = 50,
    ) -> list[HarnessArtifact]:
        """按条件查询 artifacts。"""
        run_ids: set[str] | None = None
        if project_id is not None:
            run_ids = {
                r.run_id for r in self._runs.values()
                if r.session_key and r.session_key.project_id == project_id
            }

        results = []
        for rid, arts in self._artifacts.items():
            if run_ids is not None and rid not in run_ids:
                continue
            for a in arts:
                if artifact_type and a.artifact_type.value != artifact_type:
                    continue
                results.append(a)
        results.sort(key=lambda a: a.created_at, reverse=True)
        return results[:limit]

    def list_approvals(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HarnessApproval]:
        """按条件查询 approvals。"""
        run_ids: set[str] | None = None
        if project_id is not None:
            run_ids = {
                r.run_id for r in self._runs.values()
                if r.session_key and r.session_key.project_id == project_id
            }

        results = []
        for rid, apprs in self._approvals.items():
            if run_ids is not None and rid not in run_ids:
                continue
            for a in apprs:
                if status and a.status.value != status:
                    continue
                results.append(a)
        results.sort(key=lambda a: a.requested_at, reverse=True)
        return results[:limit]

    def get_audit_stats(
        self,
        *,
        project_id: int | None = None,
        user_id: int | None = None,
    ) -> dict:
        """聚合审计统计 — 按 agent_type、status、tool 使用。"""
        runs = self.list_runs(project_id=project_id, user_id=user_id, limit=10000)
        total = len(runs)
        by_status: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        total_duration = 0.0
        duration_count = 0

        for r in runs:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            at = r.agent_type.value if r.agent_type else "unknown"
            by_agent[at] = by_agent.get(at, 0) + 1
            if r.finished_at and r.started_at:
                total_duration += r.finished_at - r.started_at
                duration_count += 1

        # Tool usage stats
        tool_counts: dict[str, int] = {}
        for r in runs:
            for step in self.get_steps(r.run_id):
                if step.step_type == StepType.TOOL_CALL:
                    tool_name = step.metadata.get("tool_name", "unknown")
                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

        return {
            "total_runs": total,
            "by_status": by_status,
            "by_agent_type": by_agent,
            "avg_duration_sec": round(total_duration / duration_count, 2) if duration_count else 0,
            "tool_usage": tool_counts,
            "failure_rate": round(by_status.get("failed", 0) / total, 3) if total else 0,
        }

    def replay_from_events(self, run_id: str, db: DBSession) -> list[dict]:
        """从 UnifiedEvent 表重建 run 的事件序列 — 持久化 replay。"""
        events = (
            db.query(UnifiedEvent)
            .filter(
                UnifiedEvent.source_type == "harness",
                UnifiedEvent.payload["run_id"].as_string() == run_id,
            )
            .order_by(UnifiedEvent.created_at.asc())
            .all()
        )
        return [
            {
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "user_id": e.user_id,
                "workspace_id": e.workspace_id,
                "project_id": e.project_id,
            }
            for e in events
        ]

    # ── 持久化到 UnifiedEvent 表 ─────────────────────────────────────────

    @staticmethod
    def _persist_event(
        db: DBSession,
        event_type: str,
        run_id: str,
        session_key: Optional[HarnessSessionKey],
        payload: dict,
    ) -> None:
        try:
            evt = UnifiedEvent(
                event_type=event_type,
                source_type="harness",
                source_id=None,
                payload={**payload, "run_id": run_id},
                user_id=session_key.user_id if session_key else None,
                workspace_id=session_key.workspace_id if session_key else None,
                project_id=session_key.project_id if session_key else None,
            )
            db.add(evt)
            db.flush()
        except Exception:
            logger.exception("Failed to persist harness event %s for run %s", event_type, run_id)
