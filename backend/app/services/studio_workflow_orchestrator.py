"""Unified Skill Studio workflow bootstrap orchestration.

Phase 2 最小落地：
- 用统一 service 收敛首轮 route / architect bootstrap / audit + governance bootstrap
- 对外统一返回 workflow_state + legacy-compatible payloads
- 把 preflight / sandbox remediation 也接到同一返回面
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import ArchitectWorkflowState
from app.services.studio_latency_policy import (
    choose_execution_strategy,
    estimate_complexity_level,
    initial_lane_statuses,
    merge_latency_metadata,
)
from app.services.studio_router import RouteResult, route_session
from app.services.studio_workflow_adapter import normalize_workflow_card, normalize_workflow_staged_edit
from app.services.studio_workflow_protocol import WorkflowStateData


@dataclass
class WorkflowBootstrapResult:
    workflow_state: dict[str, Any]
    route_status: dict[str, Any]
    assist_skills_status: dict[str, Any]
    architect_phase_status: dict[str, Any] | None = None
    audit_summary: dict[str, Any] | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    staged_edits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkflowRemediationResult:
    workflow_state: dict[str, Any]
    cards: list[dict[str, Any]] = field(default_factory=list)
    staged_edits: list[dict[str, Any]] = field(default_factory=list)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_RECOVERY_REUSE_PHASES = {"remediate", "validate", "ready"}
_RECOVERY_REUSE_ACTIONS = {
    "review_cards",
    "run_preflight",
    "run_sandbox",
    "run_targeted_rerun",
    "submit_approval",
}


def _phase_for_route(route_result: RouteResult) -> str:
    if route_result.workflow_mode == "architect_mode":
        return route_result.initial_phase or "phase_1_why"
    if route_result.next_action == "run_audit":
        return "review"
    if route_result.next_action == "start_editing":
        return "revise"
    if route_result.next_action == "collect_requirements":
        return "discover"
    return "discover"


def _build_workflow_state(
    *,
    workflow_id: str | None,
    conversation_id: int | None,
    skill_id: int | None,
    route_result: RouteResult | None = None,
    phase: str | None = None,
    next_action: str | None = None,
    session_mode: str | None = None,
    workflow_mode: str | None = None,
    route_reason: str = "",
    active_assist_skills: list[str] | None = None,
    complexity_level: str | None = None,
    execution_strategy: str | None = None,
    fast_status: str | None = None,
    deep_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_session_mode = session_mode or (route_result.session_mode if route_result else "optimize_existing_skill")
    resolved_workflow_mode = workflow_mode or (route_result.workflow_mode if route_result else "none")
    resolved_phase = phase or (_phase_for_route(route_result) if route_result else "discover")
    resolved_next_action = next_action or (route_result.next_action if route_result else "continue_chat")
    resolved_route_reason = route_reason or (route_result.route_reason if route_result else "")
    resolved_assist_skills = list(active_assist_skills or (route_result.active_assist_skills if route_result else []) or [])
    resolved_complexity = complexity_level or "medium"
    resolved_strategy = execution_strategy or "fast_then_deep"
    resolved_lane = initial_lane_statuses(resolved_strategy)
    return WorkflowStateData(
        workflow_id=workflow_id,
        session_mode=resolved_session_mode,
        workflow_mode=resolved_workflow_mode,
        phase=resolved_phase,
        next_action=resolved_next_action,
        complexity_level=resolved_complexity,
        execution_strategy=resolved_strategy,
        fast_status=fast_status or resolved_lane["fast_status"],
        deep_status=deep_status or resolved_lane["deep_status"],
        route_reason=resolved_route_reason,
        active_assist_skills=resolved_assist_skills,
        skill_id=skill_id,
        conversation_id=conversation_id,
        metadata=metadata or {},
    ).to_dict()


def _route_payload(route_result: RouteResult, *, next_action: str | None = None) -> dict[str, Any]:
    return {
        "session_mode": route_result.session_mode,
        "active_assist_skills": route_result.active_assist_skills,
        "route_reason": route_result.route_reason,
        "next_action": next_action or route_result.next_action,
        "workflow_mode": route_result.workflow_mode,
        "initial_phase": route_result.initial_phase,
    }


def _assist_payload(route_result: RouteResult) -> dict[str, Any]:
    return {
        "skills": route_result.active_assist_skills,
        "session_mode": route_result.session_mode,
    }


def _route_payload_from_workflow_state(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_mode": workflow_state.get("session_mode"),
        "active_assist_skills": workflow_state.get("active_assist_skills") or [],
        "route_reason": workflow_state.get("route_reason") or "",
        "next_action": workflow_state.get("next_action") or "continue_chat",
        "workflow_mode": workflow_state.get("workflow_mode") or "none",
        "initial_phase": workflow_state.get("phase") or "",
        "complexity_level": workflow_state.get("complexity_level") or "medium",
        "execution_strategy": workflow_state.get("execution_strategy") or "fast_then_deep",
        "fast_status": workflow_state.get("fast_status") or "pending",
        "deep_status": workflow_state.get("deep_status") or "pending",
    }


def _assist_payload_from_workflow_state(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "skills": workflow_state.get("active_assist_skills") or [],
        "session_mode": workflow_state.get("session_mode"),
    }


def _derive_latency_controls(
    *,
    route_result: RouteResult,
    user_message: str,
    skill_id: int | None,
    has_memo: bool = False,
    history_count: int = 0,
) -> tuple[str, str]:
    complexity_level = estimate_complexity_level(
        session_mode=route_result.session_mode,
        workflow_mode=route_result.workflow_mode,
        next_action=route_result.next_action,
        user_message=user_message,
        has_files=False,
        has_memo=has_memo,
        history_count=history_count,
    )
    execution_strategy = choose_execution_strategy(
        complexity_level=complexity_level,
        workflow_mode=route_result.workflow_mode,
        next_action=route_result.next_action,
    )
    if skill_id is None and complexity_level == "simple":
        complexity_level = "medium"
        execution_strategy = "fast_then_deep"
    return complexity_level, execution_strategy


def _should_reuse_recovery(recovery: dict[str, Any] | None) -> bool:
    if not isinstance(recovery, dict):
        return False
    workflow_state = recovery.get("workflow_state")
    if not isinstance(workflow_state, dict):
        return False
    cards = recovery.get("cards")
    staged_edits = recovery.get("staged_edits")
    if isinstance(cards, list) and cards:
        return True
    if isinstance(staged_edits, list) and staged_edits:
        return True
    phase = str(workflow_state.get("phase") or "")
    next_action = str(workflow_state.get("next_action") or "")
    workflow_mode = str(workflow_state.get("workflow_mode") or "")
    return (
        phase in _RECOVERY_REUSE_PHASES
        or next_action in _RECOVERY_REUSE_ACTIONS
        or workflow_mode in {"preflight_remediation", "sandbox_remediation"}
    )


def _recover_existing_bootstrap(
    db: Session,
    *,
    workflow_id: str | None,
    conversation_id: int,
    skill_id: int | None,
    user_id: int | None,
) -> WorkflowBootstrapResult | None:
    if not skill_id:
        return None

    from app.services.skill_memo_service import get_memo, sync_workflow_recovery

    memo = get_memo(db, skill_id)
    recovery = memo.get("workflow_recovery") if isinstance(memo, dict) else None
    if not _should_reuse_recovery(recovery):
        return None

    workflow_state = dict(recovery.get("workflow_state") or {})
    workflow_state["workflow_id"] = workflow_id
    workflow_state["conversation_id"] = conversation_id
    workflow_state["skill_id"] = skill_id
    if "status" not in workflow_state:
        workflow_state["status"] = "active"
    workflow_state.setdefault("complexity_level", "medium")
    workflow_state.setdefault("execution_strategy", "fast_then_deep")
    workflow_state.setdefault("fast_status", "pending")
    workflow_state.setdefault("deep_status", "pending")

    cards = list(recovery.get("cards") or [])
    staged_edits = list(recovery.get("staged_edits") or [])

    sync_workflow_recovery(
        db,
        skill_id,
        workflow_state=workflow_state,
        cards=cards,
        staged_edits=staged_edits,
        user_id=user_id,
        commit=False,
    )

    architect_phase_status: dict[str, Any] | None = None
    if workflow_state.get("workflow_mode") == "architect_mode":
        architect_phase_status = {
            "phase": workflow_state.get("phase"),
            "mode_source": workflow_state.get("session_mode"),
        }
        arch_state = (
            db.query(ArchitectWorkflowState)
            .filter(ArchitectWorkflowState.conversation_id == conversation_id)
            .first()
        )
        if arch_state:
            architect_phase_status["ooda_round"] = arch_state.ooda_round

    return WorkflowBootstrapResult(
        workflow_state=workflow_state,
        route_status=_route_payload_from_workflow_state(workflow_state),
        assist_skills_status=_assist_payload_from_workflow_state(workflow_state),
        architect_phase_status=architect_phase_status,
        cards=cards,
        staged_edits=staged_edits,
    )


def _ensure_architect_state(
    db: Session,
    *,
    conversation_id: int,
    skill_id: int | None,
    route_result: RouteResult,
) -> dict[str, Any] | None:
    if route_result.workflow_mode != "architect_mode":
        return None

    arch_state = (
        db.query(ArchitectWorkflowState)
        .filter(ArchitectWorkflowState.conversation_id == conversation_id)
        .first()
    )
    if not arch_state:
        arch_state = ArchitectWorkflowState(
            conversation_id=conversation_id,
            skill_id=skill_id,
            workflow_mode="architect_mode",
            workflow_phase=route_result.initial_phase or "phase_1_why",
        )
        db.add(arch_state)
        db.commit()
        db.refresh(arch_state)

    return {
        "phase": arch_state.workflow_phase,
        "mode_source": route_result.session_mode,
        "ooda_round": arch_state.ooda_round,
    }


async def bootstrap_workflow(
    db: Session,
    *,
    workflow_id: str | None,
    conversation_id: int,
    skill_id: int | None,
    user_message: str,
    user_id: int | None = None,
) -> WorkflowBootstrapResult:
    accepted_at = _now_iso()
    recovered = _recover_existing_bootstrap(
        db,
        workflow_id=workflow_id,
        conversation_id=conversation_id,
        skill_id=skill_id,
        user_id=user_id,
    )
    if recovered is not None:
        recovered.workflow_state["metadata"] = merge_latency_metadata(
            recovered.workflow_state.get("metadata"),
            accepted_at=accepted_at,
            classified_at=_now_iso(),
        )
        return recovered

    route_result = route_session(db, skill_id=skill_id, user_message=user_message)
    complexity_level, execution_strategy = _derive_latency_controls(
        route_result=route_result,
        user_message=user_message,
        skill_id=skill_id,
        has_memo=bool(skill_id),
    )
    architect_phase_status = _ensure_architect_state(
        db,
        conversation_id=conversation_id,
        skill_id=skill_id,
        route_result=route_result,
    )

    phase = architect_phase_status["phase"] if architect_phase_status else _phase_for_route(route_result)
    next_action = route_result.next_action
    audit_summary: dict[str, Any] | None = None
    cards: list[dict[str, Any]] = []
    staged_edits: list[dict[str, Any]] = []

    if route_result.next_action == "run_audit" and skill_id:
        from app.services.studio_auditor import run_audit
        from app.services.studio_governance import generate_governance_actions

        audit_result = await run_audit(db, skill_id)
        audit_summary = {
            "verdict": audit_result.verdict,
            "issues": audit_result.issues,
            "recommended_path": audit_result.recommended_path,
            "audit_id": getattr(audit_result, "audit_id", None),
        }
        if audit_result.verdict in {"needs_work", "poor"}:
            gov_result = await generate_governance_actions(
                db,
                skill_id,
                audit_id=getattr(audit_result, "audit_id", None),
            )
            cards = list(gov_result.cards or [])
            staged_edits = list(gov_result.staged_edits or [])
            if cards or staged_edits:
                next_action = "review_cards"
        else:
            next_action = "continue_chat"

    workflow_state = _build_workflow_state(
        workflow_id=workflow_id,
        conversation_id=conversation_id,
        skill_id=skill_id,
        route_result=route_result,
        phase=phase,
        next_action=next_action,
        complexity_level=complexity_level,
        execution_strategy=execution_strategy,
        metadata=merge_latency_metadata(
            None,
            accepted_at=accepted_at,
            classified_at=_now_iso(),
        ),
    )
    if skill_id:
        from app.services.skill_memo_service import sync_workflow_recovery

        sync_workflow_recovery(
            db,
            skill_id,
            workflow_state=workflow_state,
            cards=cards,
            staged_edits=staged_edits,
            user_id=user_id,
            commit=False,
        )

    route_status = _route_payload(route_result, next_action=next_action)
    route_status.update({
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        "fast_status": workflow_state.get("fast_status"),
        "deep_status": workflow_state.get("deep_status"),
    })

    return WorkflowBootstrapResult(
        workflow_state=workflow_state,
        route_status=route_status,
        assist_skills_status=_assist_payload(route_result),
        architect_phase_status=architect_phase_status,
        audit_summary=audit_summary,
        cards=cards,
        staged_edits=staged_edits,
    )


def bootstrap_preflight_remediation(
    db: Session,
    *,
    workflow_id: str | None,
    skill_id: int,
    result: dict[str, Any],
    user_id: int | None = None,
    commit: bool = False,
) -> WorkflowRemediationResult:
    from app.services.preflight_governance import build_preflight_governance

    governance_result = build_preflight_governance(db, skill_id=skill_id, result=result)
    cards = [
        normalize_workflow_card(card, source_type="preflight_remediation", phase="remediate", workflow_id=workflow_id)
        for card in governance_result.cards
    ]
    staged_edits = [
        normalize_workflow_staged_edit(edit, source_type="preflight_remediation", workflow_id=workflow_id)
        for edit in governance_result.staged_edits
    ]
    workflow_state = _build_workflow_state(
        workflow_id=workflow_id,
        conversation_id=None,
        skill_id=skill_id,
        session_mode="optimize_existing_skill",
        workflow_mode="preflight_remediation",
        phase="remediate",
        next_action="review_cards" if cards or staged_edits else "continue_chat",
        complexity_level="high",
        execution_strategy="deep_resume",
        fast_status="completed",
        deep_status="pending" if cards or staged_edits else "not_requested",
        route_reason="preflight_failed",
        metadata=merge_latency_metadata(
            {"source": "preflight_remediation"},
            accepted_at=_now_iso(),
            classified_at=_now_iso(),
        ),
    )
    from app.services.skill_memo_service import sync_workflow_recovery

    sync_workflow_recovery(
        db,
        skill_id,
        workflow_state=workflow_state,
        cards=cards,
        staged_edits=staged_edits,
        user_id=user_id,
        commit=commit,
    )
    return WorkflowRemediationResult(
        workflow_state=workflow_state,
        cards=cards,
        staged_edits=staged_edits,
    )


async def bootstrap_sandbox_remediation(
    db: Session,
    *,
    workflow_id: str | None,
    skill_id: int,
    report: Any,
    user_id: int | None = None,
    commit: bool = False,
) -> WorkflowRemediationResult:
    from app.services.sandbox_governance import build_sandbox_report_governance

    governance_result = await build_sandbox_report_governance(db, skill_id=skill_id, report=report)
    cards = []
    for card in governance_result.cards:
        normalized = card if isinstance(card, dict) and card.get("source") else normalize_workflow_card(
            card,
            source_type="sandbox_remediation",
            phase="remediate",
            workflow_id=workflow_id,
        )
        if workflow_id and isinstance(normalized, dict):
            normalized.setdefault("workflow_id", workflow_id)
        cards.append(normalized)
    staged_edits = []
    for edit in governance_result.staged_edits:
        normalized = edit if isinstance(edit, dict) and edit.get("source") else normalize_workflow_staged_edit(
            edit,
            source_type="sandbox_remediation",
            workflow_id=workflow_id,
        )
        if workflow_id and isinstance(normalized, dict):
            normalized.setdefault("workflow_id", workflow_id)
        staged_edits.append(normalized)
    workflow_state = _build_workflow_state(
        workflow_id=workflow_id,
        conversation_id=None,
        skill_id=skill_id,
        session_mode="optimize_existing_skill",
        workflow_mode="sandbox_remediation",
        phase="remediate",
        next_action="review_cards" if cards or staged_edits else "continue_chat",
        complexity_level="high",
        execution_strategy="deep_resume",
        fast_status="completed",
        deep_status="pending" if cards or staged_edits else "not_requested",
        route_reason="sandbox_failed",
        metadata=merge_latency_metadata(
            {"source": "sandbox_remediation", "report_id": getattr(report, "id", None)},
            accepted_at=_now_iso(),
            classified_at=_now_iso(),
        ),
    )
    from app.services.skill_memo_service import sync_workflow_recovery

    sync_workflow_recovery(
        db,
        skill_id,
        workflow_state=workflow_state,
        cards=cards,
        staged_edits=staged_edits,
        user_id=user_id,
        commit=commit,
    )
    return WorkflowRemediationResult(
        workflow_state=workflow_state,
        cards=cards,
        staged_edits=staged_edits,
    )
