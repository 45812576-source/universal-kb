"""测试流 API — resolve-entry / fork / confirm。"""
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api_envelope import raise_api_error
from app.database import get_db
from app.dependencies import get_current_user
from app.models.skill_governance import TestCasePlanDraft
from app.models.user import User
from app.services.skill_governance_service import (
    assert_skill_governance_access,
    ok,
)
from app.services.test_flow_cases import (
    build_plan_summary,
    confirm_case_plan,
    fork_case_plan,
)
from app.services.test_flow_trigger import resolve_test_flow_entry
from app.services.test_flow_workflow import (
    emit_test_flow_blocked,
    emit_test_flow_case_draft,
    emit_test_flow_resolution,
)

router = APIRouter(prefix="/api/test-flow", tags=["test-flow"])


class ResolveEntryRequest(BaseModel):
    content: str = ""
    entry_source: str = "sandbox_chat"
    conversation_id: Optional[int] = None
    selected_skill_id: Optional[int] = None
    mentioned_skill_ids: list[int] = Field(default_factory=list)
    candidate_skills: list[dict[str, Any]] = Field(default_factory=list)


class ForkCasePlanRequest(BaseModel):
    mode: str = "revise"
    entry_source: Optional[str] = None
    conversation_id: Optional[int] = None


@router.post("/resolve-entry")
def resolve_entry(
    req: ResolveEntryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """触发解析 — 多 Skill 分流、阻断分流、已有 plan 检测。"""
    result = resolve_test_flow_entry(db, req.model_dump())

    # workflow event: 记录每次解析结果
    action = result.get("action", "chat_default")
    skill_info = result.get("skill")
    skill_id = skill_info["id"] if isinstance(skill_info, dict) else None
    emit_test_flow_resolution(
        db, skill_id=skill_id, user_id=user.id,
        action=action, reason=result.get("reason"),
        payload_extra={
            "entry_source": req.entry_source,
            "blocked_stage": result.get("blocked_stage"),
            "blocked_before": result.get("blocked_before"),
            "case_generation_allowed": result.get("case_generation_allowed"),
            "quality_evaluation_started": result.get("quality_evaluation_started"),
        },
    )
    if action == "mount_blocked":
        emit_test_flow_blocked(
            db, skill_id=skill_id or 0, user_id=user.id,
            blocking_issues=result.get("blocking_issues", []),
            mount_cta=result.get("mount_cta"),
            gate_summary=result.get("gate_summary"),
            primary_action=result.get("primary_action"),
        )

    return ok(result)


@router.post("/sandbox-case-plans/{plan_id}/fork")
def fork_plan(
    plan_id: int,
    req: ForkCasePlanRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """基于现有 plan fork 新版本。"""
    source = db.get(TestCasePlanDraft, plan_id)
    if not source:
        raise_api_error(404, "test_flow.plan_not_found", "Plan not found", {"plan_id": plan_id})
    assert_skill_governance_access(db, source.skill_id, user)

    new_plan = fork_case_plan(
        db,
        plan_id=plan_id,
        mode=req.mode,
        user_id=user.id,
        entry_source=req.entry_source,
        conversation_id=req.conversation_id,
    )
    db.commit()

    emit_test_flow_case_draft(
        db, skill_id=source.skill_id, user_id=user.id,
        plan_id=new_plan.id, plan_version=new_plan.plan_version,
        generation_mode=req.mode, source_plan_id=plan_id,
    )

    return ok({
        "plan_id": new_plan.id,
        "plan_version": new_plan.plan_version,
        "source_plan_id": new_plan.source_plan_id,
        "generation_mode": new_plan.generation_mode,
        "case_count": new_plan.case_count,
        "summary": build_plan_summary(new_plan),
    })


@router.post("/sandbox-case-plans/{plan_id}/confirm")
def confirm_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户确认 plan 可执行。"""
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "test_flow.plan_not_found", "Plan not found", {"plan_id": plan_id})
    assert_skill_governance_access(db, plan.skill_id, user)

    result = confirm_case_plan(db, plan_id)
    db.commit()

    emit_test_flow_case_draft(
        db, skill_id=plan.skill_id, user_id=user.id,
        plan_id=plan_id, plan_version=plan.plan_version,
        generation_mode="confirm",
    )

    return ok(result)
