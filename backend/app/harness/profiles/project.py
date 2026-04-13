"""ProjectOrchestratorProfile — 项目级统一调度 Profile。

G5 核心交付：
- 项目 workspace 作为子 session（需求 ws / 开发 ws）
- 统一 run 追踪：plan/apply/sync/handoff/report 全部创建 HarnessRun
- 交接物（handoff/report）记录为 HarnessArtifact
- 项目上下文（ProjectContext）记录为 HarnessMemoryRef

使用方：projects.py router 通过 project_orchestrator 调用。
"""
from __future__ import annotations

import logging
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
    HarnessSessionKey,
    HarnessStep,
    RunStatus,
    StepType,
)
from app.harness.events import EventName, HarnessEvent, emit
from app.harness.gateway import get_session_store
from app.harness.session_store import SessionStore

logger = logging.getLogger(__name__)


class ProjectOrchestratorProfile:
    """项目级统一调度 Profile。

    职责:
    1. 为项目创建主 HarnessSession（agent_type=PROJECT）
    2. 为每个 workspace 成员创建子 session（记录在 metadata.sub_sessions）
    3. 所有项目操作（plan/apply/sync/handoff/report）通过 run 追踪
    4. 提供项目级 run 查询与审计入口
    """

    def __init__(self, store: Optional[SessionStore] = None):
        self.store = store or get_session_store()

    def _session_key(self, user_id: int, project_id: int) -> HarnessSessionKey:
        return HarnessSessionKey(
            user_id=user_id,
            agent_type=AgentType.PROJECT,
            project_id=project_id,
        )

    def ensure_project_session(
        self,
        user_id: int,
        project_id: int,
        db: Session,
    ) -> tuple[str, str]:
        """确保项目主 session 存在，返回 (session_id, compound_key)。"""
        session_key = self._session_key(user_id, project_id)
        session = self.store.create_or_get_session(
            session_key, agent_type=AgentType.PROJECT, db=db,
        )
        return session.session_id, session_key.compound_key

    def register_sub_session(
        self,
        user_id: int,
        project_id: int,
        workspace_id: int,
        workspace_type: str,
        db: Session,
    ) -> str:
        """将 workspace 注册为项目的子 session。

        子 session 信息存储在主 session 的 metadata.sub_sessions 中。
        返回子 session 的 compound_key。
        """
        session_key = self._session_key(user_id, project_id)
        session = self.store.create_or_get_session(
            session_key, agent_type=AgentType.PROJECT, db=db,
        )

        sub_sessions: list[dict] = session.metadata.setdefault("sub_sessions", [])
        # 检查是否已注册
        for sub in sub_sessions:
            if sub.get("workspace_id") == workspace_id:
                return sub.get("compound_key", "")

        # 根据 workspace_type 决定子 session 的 agent_type
        _agent_map = {
            "chat": AgentType.CHAT,
            "opencode": AgentType.DEV_STUDIO,
            "skill_studio": AgentType.SKILL_STUDIO,
            "sandbox": AgentType.SANDBOX,
        }
        sub_agent_type = _agent_map.get(workspace_type, AgentType.CHAT)

        sub_key = HarnessSessionKey(
            user_id=user_id,
            agent_type=sub_agent_type,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        sub_session = self.store.create_or_get_session(
            sub_key, agent_type=sub_agent_type, db=db,
        )

        sub_sessions.append({
            "workspace_id": workspace_id,
            "workspace_type": workspace_type,
            "agent_type": sub_agent_type.value,
            "session_id": sub_session.session_id,
            "compound_key": sub_key.compound_key,
        })

        return sub_key.compound_key

    def create_run(
        self,
        user_id: int,
        project_id: int,
        operation: str,
        db: Session,
    ) -> HarnessRun:
        """为项目操作创建 HarnessRun（plan/apply/sync/handoff/report）。"""
        session_key = self._session_key(user_id, project_id)
        session = self.store.create_or_get_session(
            session_key, agent_type=AgentType.PROJECT, db=db,
        )
        run = HarnessRun(
            request_id="",
            session_id=session.session_id,
            session_key=session_key,
            agent_type=AgentType.PROJECT,
            metadata={"operation": operation, "project_id": project_id},
        )
        self.store.create_run(run, db=db)
        self.store.update_run_status(run.run_id, RunStatus.RUNNING, db=db)
        return run

    def complete_run(
        self, run_id: str, db: Session, *, error: Optional[str] = None,
    ) -> None:
        """完成 run。"""
        if error:
            self.store.update_run_status(run_id, RunStatus.FAILED, error=error, db=db)
        else:
            self.store.update_run_status(run_id, RunStatus.COMPLETED, db=db)

    def get_project_runs(self, project_id: int) -> list[HarnessRun]:
        """获取项目相关的所有 runs（内存查询）。"""
        results = []
        for run in self.store._runs.values():
            if run.session_key and run.session_key.project_id == project_id:
                results.append(run)
        results.sort(key=lambda r: r.started_at, reverse=True)
        return results

    def get_project_artifacts(self, project_id: int) -> list[HarnessArtifact]:
        """获取项目相关的所有 artifacts。"""
        run_ids = {r.run_id for r in self.get_project_runs(project_id)}
        results = []
        for rid in run_ids:
            results.extend(self.store.get_artifacts(rid))
        results.sort(key=lambda a: a.created_at, reverse=True)
        return results

    def get_project_memory_refs(self, project_id: int) -> list[HarnessMemoryRef]:
        """获取项目相关的所有 memory refs。"""
        run_ids = {r.run_id for r in self.get_project_runs(project_id)}
        results = []
        for rid in run_ids:
            results.extend(self.store.get_memory_refs(rid))
        return results

    def get_project_sub_sessions(self, user_id: int, project_id: int) -> list[dict]:
        """获取项目的子 session 列表。"""
        session_key = self._session_key(user_id, project_id)
        session = self.store.get_session(session_key)
        if not session:
            return []
        return session.metadata.get("sub_sessions", [])

    def get_project_audit_summary(self, project_id: int) -> dict:
        """项目级审计摘要 — runs 统计、artifact 统计、失败率。"""
        runs = self.get_project_runs(project_id)
        total = len(runs)
        completed = sum(1 for r in runs if r.status == RunStatus.COMPLETED)
        failed = sum(1 for r in runs if r.status == RunStatus.FAILED)

        # 按 agent_type 聚合
        agent_counts: dict[str, int] = {}
        for r in runs:
            at = r.agent_type.value if r.agent_type else "unknown"
            agent_counts[at] = agent_counts.get(at, 0) + 1

        # 按 operation 聚合
        op_counts: dict[str, int] = {}
        for r in runs:
            op = r.metadata.get("operation", "unknown")
            op_counts[op] = op_counts.get(op, 0) + 1

        artifacts = self.get_project_artifacts(project_id)
        artifact_counts: dict[str, int] = {}
        for a in artifacts:
            at = a.artifact_type.value
            artifact_counts[at] = artifact_counts.get(at, 0) + 1

        return {
            "project_id": project_id,
            "total_runs": total,
            "completed_runs": completed,
            "failed_runs": failed,
            "failure_rate": round(failed / total, 3) if total > 0 else 0,
            "by_agent_type": agent_counts,
            "by_operation": op_counts,
            "total_artifacts": len(artifacts),
            "artifacts_by_type": artifact_counts,
        }


# 模块级单例
project_orchestrator = ProjectOrchestratorProfile()
