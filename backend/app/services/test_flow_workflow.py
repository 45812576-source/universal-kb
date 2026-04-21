"""测试流 Workflow 适配 — 把 test flow 状态映射到统一 workflow event + state。"""
from typing import Any

from sqlalchemy.orm import Session

from app.services import event_bus

# ── test flow → workflow phase 映射 ─────────────────────────────────────────

_ACTION_TO_PHASE: dict[str, str] = {
    "chat_default": "idle",
    "pick_skill": "choose_skill",
    "mount_blocked": "blocked",
    "choose_existing_plan": "case_branch",
    "generate_cases": "case_edit",
}


def test_flow_phase(action: str) -> str:
    return _ACTION_TO_PHASE.get(action, "idle")


# ── workflow metadata builder ────────────────────────────────────────────────


def build_test_flow_metadata(
    entry_source: str | None = None,
    matched_skill_ids: list[int] | None = None,
    selected_skill_id: int | None = None,
    phase_status: str | None = None,
    blocking_issues: list[str] | None = None,
    latest_plan_summary: dict[str, Any] | None = None,
    decision_mode: str | None = None,
    pending_plan_id: int | None = None,
    pending_plan_version: int | None = None,
    run_link_id: int | None = None,
    sandbox_session_id: int | None = None,
    report_id: int | None = None,
) -> dict[str, Any]:
    """构造 workflow_state.metadata.test_flow 子结构。"""
    return {
        k: v for k, v in {
            "entry_source": entry_source,
            "matched_skill_ids": matched_skill_ids,
            "selected_skill_id": selected_skill_id,
            "phase_status": phase_status,
            "blocking_issues": blocking_issues,
            "latest_plan_summary": latest_plan_summary,
            "decision_mode": decision_mode,
            "pending_plan_id": pending_plan_id,
            "pending_plan_version": pending_plan_version,
            "run_link_id": run_link_id,
            "sandbox_session_id": sandbox_session_id,
            "report_id": report_id,
        }.items() if v is not None
    }


# ── event emitters ───────────────────────────────────────────────────────────


def emit_test_flow_resolution(
    db: Session,
    skill_id: int | None,
    user_id: int,
    action: str,
    reason: str | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> None:
    """解析完成后发射 test_flow_resolution 事件。"""
    payload: dict[str, Any] = {"action": action, "reason": reason}
    if payload_extra:
        payload.update(payload_extra)
    event_bus.emit(
        db,
        event_type="test_flow_resolution",
        source_type="test_flow",
        source_id=skill_id,
        payload=payload,
        user_id=user_id,
    )


def emit_test_flow_blocked(
    db: Session,
    skill_id: int,
    user_id: int,
    blocking_issues: list[str],
    mount_cta: str | None = None,
    gate_summary: str | None = None,
    primary_action: str | None = None,
) -> None:
    """挂载阻断事件。"""
    payload: dict[str, Any] = {
        "blocking_issues": blocking_issues,
        "mount_cta": mount_cta,
    }
    if gate_summary is not None:
        payload["gate_summary"] = gate_summary
    if primary_action is not None:
        payload["primary_action"] = primary_action
    event_bus.emit(
        db,
        event_type="test_flow_blocked",
        source_type="test_flow",
        source_id=skill_id,
        payload=payload,
        user_id=user_id,
    )


def emit_test_flow_case_draft(
    db: Session,
    skill_id: int,
    user_id: int,
    plan_id: int,
    plan_version: int,
    generation_mode: str | None = None,
    source_plan_id: int | None = None,
) -> None:
    """case plan 产出事件（fork / generate / confirm）。"""
    event_bus.emit(
        db,
        event_type="test_flow_case_draft",
        source_type="test_flow",
        source_id=skill_id,
        payload={
            "plan_id": plan_id,
            "plan_version": plan_version,
            "generation_mode": generation_mode,
            "source_plan_id": source_plan_id,
        },
        user_id=user_id,
    )


def emit_test_flow_execution_started(
    db: Session,
    skill_id: int,
    user_id: int,
    plan_id: int,
    plan_version: int,
    sandbox_session_id: int,
    decision_mode: str | None = None,
) -> None:
    """materialize 完成、执行开始。"""
    event_bus.emit(
        db,
        event_type="test_flow_execution_started",
        source_type="test_flow",
        source_id=skill_id,
        payload={
            "plan_id": plan_id,
            "plan_version": plan_version,
            "sandbox_session_id": sandbox_session_id,
            "decision_mode": decision_mode,
        },
        user_id=user_id,
    )
