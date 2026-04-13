"""SandboxAgentProfile — 沙盒测试接入 Harness 统一运行时。

G3 核心交付：
- Sandbox run 阶段的 case_execution 改用 AgentRuntime（sandbox_mode=true）
- 每条 case 执行产生 HarnessStep，报告可追溯到真实 run/step
- 保留 evidence wizard（Q1/Q2/Q3 不变）
- 禁止 mock 输入和自动补全测试数据

Phase 1 策略：
- case_execution 中每条用例的 LLM 调用包装为 HarnessStep(type=MODEL_CALL)
- 整体 run 记录到 HarnessRun
- 报告记录到 HarnessArtifact(type=REPORT)
- 不改变 sandbox_interactive.py 的 API 路由结构，仅替换内部执行逻辑
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.harness.contracts import (
    AgentType,
    ArtifactType,
    HarnessArtifact,
    HarnessMemoryRef,
    HarnessRun,
    HarnessStep,
    RunStatus,
    StepType,
)
from app.harness.gateway import get_session_store
from app.harness.session_store import SessionStore
from app.models.sandbox import (
    CaseVerdict,
    SandboxTestCase,
    SandboxTestReport,
    SandboxTestSession,
)

logger = logging.getLogger(__name__)


class SandboxAgentProfile:
    """沙盒测试 Harness Profile。

    职责:
    1. 为每次 sandbox run 创建 HarnessRun
    2. 每条 case 执行记录为 HarnessStep(type=MODEL_CALL)
    3. 报告记录为 HarnessArtifact(type=REPORT)
    4. 证据追溯：report.harness_run_id 关联真实 run
    """

    def __init__(self, store: Optional[SessionStore] = None):
        self.store = store or get_session_store()

    def begin_run(
        self,
        sandbox_session: SandboxTestSession,
        user_id: int,
        db: Session,
    ) -> HarnessRun:
        """开始一次 sandbox run — 在 run_tests 入口调用。"""
        from app.harness.contracts import HarnessSessionKey

        session_key = HarnessSessionKey(
            user_id=user_id,
            agent_type=AgentType.SANDBOX,
            target_type=sandbox_session.target_type,
            target_id=sandbox_session.target_id,
        )
        harness_session = self.store.create_or_get_session(
            session_key,
            agent_type=AgentType.SANDBOX,
            db=db,
        )
        run = HarnessRun(
            session_id=harness_session.session_id,
            session_key=session_key,
            agent_type=AgentType.SANDBOX,
            metadata={
                "sandbox_session_id": sandbox_session.id,
                "target_type": sandbox_session.target_type,
                "target_id": sandbox_session.target_id,
                "target_name": sandbox_session.target_name or "",
            },
        )
        self.store.create_run(run, db=db)
        self.store.update_run_status(run.run_id, RunStatus.RUNNING, db=db)

        # 写回 sandbox session 供报告追溯
        step_statuses = dict(sandbox_session.step_statuses or {})
        step_statuses["_harness_run_id"] = run.run_id
        step_statuses["_harness_session_id"] = harness_session.session_id
        sandbox_session.step_statuses = step_statuses
        flag_modified(sandbox_session, "step_statuses")
        db.flush()

        logger.info(
            "SandboxAgentProfile: began run %s for sandbox_session %s (target=%s:%s)",
            run.run_id, sandbox_session.id, sandbox_session.target_type, sandbox_session.target_id,
        )
        return run

    def record_case_step(
        self,
        run: HarnessRun,
        case: SandboxTestCase,
        db: Session,
    ) -> HarnessStep:
        """记录单条 case 执行为 HarnessStep。"""
        step = HarnessStep(
            run_id=run.run_id,
            step_type=StepType.MODEL_CALL,
            seq=case.case_index,
            input_summary=f"case[{case.case_index}] row={case.row_visibility} field={case.field_output_semantic}",
            metadata={
                "sandbox_case_id": case.id if case.id else None,
                "case_index": case.case_index,
                "row_visibility": case.row_visibility,
                "field_output_semantic": case.field_output_semantic,
                "tool_precondition": case.tool_precondition,
                "verdict": case.verdict.value if case.verdict else None,
                "execution_duration_ms": case.execution_duration_ms,
            },
        )
        if case.verdict == CaseVerdict.SKIPPED:
            step.output_summary = "SKIPPED: " + (case.verdict_reason or "")
        elif case.verdict == CaseVerdict.ERROR:
            step.error = case.verdict_reason
            step.output_summary = "ERROR"
        else:
            step.output_summary = (case.llm_response or "")[:500]

        step.finished_at = time.time()
        self.store.add_step(step, db=db)
        return step

    def record_evidence_ref(
        self,
        run: HarnessRun,
        sandbox_session: SandboxTestSession,
        db: Session,
    ) -> None:
        """记录证据引用 — 将 Q1/Q2/Q3 证据作为 HarnessMemoryRef。"""
        # 输入槽位证据
        for slot in (sandbox_session.detected_slots or []):
            if slot.get("chosen_source") and slot.get("evidence_status") == "verified":
                self.store.add_memory_ref(HarnessMemoryRef(
                    run_id=run.run_id,
                    ref_type="sandbox_evidence",
                    summary=f"Q1 slot={slot.get('slot_key')} source={slot.get('chosen_source')}",
                    metadata={"evidence_type": "input_slot", **slot},
                ))

        # 工具确认证据
        for tool in (sandbox_session.tool_review or []):
            if tool.get("confirmed"):
                self.store.add_memory_ref(HarnessMemoryRef(
                    run_id=run.run_id,
                    ref_type="sandbox_evidence",
                    summary=f"Q2 tool={tool.get('tool_name')} confirmed",
                    metadata={"evidence_type": "tool_provenance", **tool},
                ))

        # 权限快照证据
        for perm in (sandbox_session.permission_snapshot or []):
            if perm.get("confirmed"):
                self.store.add_memory_ref(HarnessMemoryRef(
                    run_id=run.run_id,
                    ref_type="sandbox_evidence",
                    summary=f"Q3 table={perm.get('table_name')} row={perm.get('row_visibility')}",
                    metadata={"evidence_type": "permission_snapshot", **perm},
                ))

    def record_report_artifact(
        self,
        run: HarnessRun,
        report: SandboxTestReport,
        db: Session,
    ) -> HarnessArtifact:
        """将测试报告记录为 HarnessArtifact。"""
        artifact = HarnessArtifact(
            run_id=run.run_id,
            artifact_type=ArtifactType.REPORT,
            name=f"sandbox_report_{report.id}",
            content_ref=f"sandbox_test_reports:{report.id}",
            metadata={
                "report_id": report.id,
                "target_type": report.target_type,
                "target_id": report.target_id,
                "quality_passed": report.quality_passed,
                "usability_passed": report.usability_passed,
                "anti_hallucination_passed": report.anti_hallucination_passed,
                "approval_eligible": report.approval_eligible,
                "report_hash": report.report_hash,
            },
        )
        self.store.add_artifact(artifact, db=db)
        return artifact

    def finish_run(
        self,
        run: HarnessRun,
        success: bool,
        db: Session,
        *,
        error: Optional[str] = None,
    ) -> None:
        """完成 sandbox run。"""
        if success:
            self.store.update_run_status(run.run_id, RunStatus.COMPLETED, db=db)
        else:
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=error, db=db)
        logger.info(
            "SandboxAgentProfile: finished run %s status=%s",
            run.run_id, "completed" if success else "failed",
        )


# 模块级单例
sandbox_profile = SandboxAgentProfile()
