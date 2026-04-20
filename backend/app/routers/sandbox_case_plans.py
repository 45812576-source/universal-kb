"""Sandbox Workbench 测试计划 API（Step 1：readiness / latest / review 收口）。"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api_envelope import raise_api_error
from app.database import get_db
from app.dependencies import get_current_user
from app.models.skill_governance import TestCaseDraft, TestCasePlanDraft
from app.models.user import User
from app.services.skill_governance_service import (
    assert_skill_governance_access,
    ensure_permission_declaration_prompt_sync,
    generate_permission_case_plan,
    latest_bundle,
    latest_case_plan,
    latest_declaration,
    materialize_permission_case_plan,
    ok,
    permission_case_plan_readiness,
    permission_case_plan_state,
    permission_contract_review_summary,
    resolve_workspace_id,
    serialize_case_draft,
    serialize_case_plan,
)
from app.services.skill_governance_jobs import (
    create_governance_job,
    process_governance_job,
    serialize_governance_job,
    session_factory_for_current_bind,
)

router = APIRouter(prefix="/api/sandbox-case-plans", tags=["sandbox-case-plans"])


class GenerateSandboxCasePlanRequest(BaseModel):
    mode: str = "permission_minimal"
    risk_focus: list[str] = []
    max_case_count: int = Field(default=10, ge=1, le=50)
    async_job: bool = False
    generation_mode: Optional[str] = None
    source_plan_id: Optional[int] = None
    entry_source: Optional[str] = None
    conversation_id: Optional[int] = None


class ReviewSandboxCasePlanRequest(BaseModel):
    accepted_case_ids: list[int] = []
    discarded_case_ids: list[int] = []


class UpdateSandboxCaseDraftRequest(BaseModel):
    status: Optional[str] = None
    test_goal: Optional[str] = Field(default=None, min_length=1)
    test_input: Optional[str] = Field(default=None, min_length=1)
    expected_behavior: Optional[str] = Field(default=None, min_length=1)
    target_role_ref: Optional[int] = None
    asset_ref: Optional[str] = None
    source_refs: Optional[list[str]] = None
    source_verification_status: Optional[str] = None
    data_source_policy: Optional[str] = None


class MaterializeSandboxCasePlanRequest(BaseModel):
    sandbox_session_id: Optional[int] = None
    entry_source: Optional[str] = None
    decision_mode: Optional[str] = None
    conversation_id: Optional[int] = None
    workflow_id: Optional[int] = None


@router.get("/{skill_id}/readiness")
def get_sandbox_case_plan_readiness(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    return ok({
        "skill_id": skill_id,
        "readiness": readiness,
    })


@router.get("/{skill_id}/latest")
def get_latest_sandbox_case_plan(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    plan = latest_case_plan(db, skill_id)
    plan_state = permission_case_plan_state(
        db,
        skill_id,
        plan=plan,
        declaration=declaration,
        bundle=bundle,
    )
    plan_data = serialize_case_plan(plan)
    if plan_data and plan:
        plan_data["summary_json"] = plan.summary_json
        plan_data["source_plan_id"] = plan.source_plan_id
        plan_data["generation_mode"] = plan.generation_mode
        plan_data["latest_materialized_session_id"] = plan.latest_materialized_session_id
    return ok({
        "skill_id": skill_id,
        "readiness": readiness,
        "plan": plan_data,
        "cases": [serialize_case_draft(case) for case in list(plan.cases or [])] if plan else [],
        "plan_state": plan_state,
    })


@router.post("/{skill_id}/generate")
def generate_sandbox_case_plan(
    skill_id: int,
    req: GenerateSandboxCasePlanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    if not readiness["ready"]:
        raise_api_error(
            400,
            "sandbox.permission_declaration_not_ready",
            "需先完成权限声明后才能生成测试集",
            {"skill_id": skill_id, "blocking_issues": readiness["blocking_issues"]},
        )
    if req.async_job:
        job = create_governance_job(
            db,
            skill_id=skill_id,
            workspace_id=workspace_id,
            job_type="permission_case_plan_generation",
            payload={
                "focus_mode": req.mode,
                "max_cases": req.max_case_count,
                "risk_focus": req.risk_focus,
                "generation_mode": req.generation_mode,
                "source_plan_id": req.source_plan_id,
                "entry_source": req.entry_source,
                "conversation_id": req.conversation_id,
            },
            created_by=user.id,
        )
        db.commit()
        background_tasks.add_task(process_governance_job, job.id, session_factory_for_current_bind(db))
        return ok({
            "job_id": job.id,
            "status": "queued",
            "job": serialize_governance_job(job),
        })
    plan = generate_permission_case_plan(
        db,
        skill,
        user,
        workspace_id=workspace_id,
        focus_mode=req.mode,
        max_cases=req.max_case_count,
    )
    # 写入测试流扩展字段
    if req.generation_mode:
        plan.generation_mode = req.generation_mode
    if req.source_plan_id:
        plan.source_plan_id = req.source_plan_id
    if req.entry_source:
        plan.entry_source = req.entry_source
    if req.conversation_id is not None:
        plan.conversation_id = req.conversation_id
    db.commit()
    return ok({
        "job_id": (
            f"case-planner-{skill_id}-"
            f"{readiness['current_skill_content_version']}-"
            f"{readiness['governance_version']}-"
            f"{readiness['permission_declaration_version']}"
        ),
        "status": "queued",
        "plan_id": plan.id,
    })


@router.put("/{plan_id}/review")
def review_sandbox_case_plan(
    plan_id: int,
    req: ReviewSandboxCasePlanRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"plan_id": plan_id})
    assert_skill_governance_access(db, plan.skill_id, user)
    accepted = set(req.accepted_case_ids)
    discarded = set(req.discarded_case_ids)
    if accepted & discarded:
        raise_api_error(
            400,
            "sandbox.review_case_ids_overlap",
            "accepted_case_ids and discarded_case_ids overlap",
            {"plan_id": plan_id, "overlap_case_ids": sorted(accepted & discarded)},
        )
    updated_count = 0
    for case in list(plan.cases or []):
        if case.id in accepted:
            case.status = "adopted"
            updated_count += 1
        elif case.id in discarded:
            case.status = "discarded"
            updated_count += 1
    db.commit()
    return ok({
        "plan_id": plan.id,
        "updated_count": updated_count,
        "accepted_case_ids": sorted(accepted),
        "discarded_case_ids": sorted(discarded),
    })


@router.put("/{plan_id}/cases/{case_id}")
def update_sandbox_case_draft(
    plan_id: int,
    case_id: int,
    req: UpdateSandboxCaseDraftRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    blocked_fields = {
        "target_role_ref",
        "asset_ref",
        "source_refs",
        "source_verification_status",
        "data_source_policy",
    }
    data = req.model_dump(exclude_unset=True)
    invalid = [field for field in blocked_fields if field in data]
    if invalid:
        raise_api_error(
            400,
            "sandbox.case_draft_blocked_fields",
            f"blocked fields: {', '.join(sorted(invalid))}",
            {"plan_id": plan_id, "case_id": case_id, "blocked_fields": sorted(invalid)},
        )
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"plan_id": plan_id})
    assert_skill_governance_access(db, plan.skill_id, user)
    case = db.get(TestCaseDraft, case_id)
    if not case or case.plan_id != plan_id:
        raise_api_error(404, "sandbox.case_draft_not_found", "Case draft not found", {"plan_id": plan_id, "case_id": case_id})
    if "status" in data:
        case.status = data["status"]
    if "test_input" in data:
        case.prompt = data["test_input"]
        case.edited_by_user = True
    if "expected_behavior" in data:
        case.expected_behavior = data["expected_behavior"]
        case.edited_by_user = True
    if "test_goal" in data:
        refs = list(case.source_refs_json or [])
        refs.append({"type": "manual_test_goal", "value": data["test_goal"]})
        case.source_refs_json = refs
        case.edited_by_user = True
    db.commit()
    db.refresh(case)
    return ok({"item": serialize_case_draft(case)})


@router.post("/{plan_id}/materialize")
def materialize_sandbox_case_plan(
    plan_id: int,
    req: MaterializeSandboxCasePlanRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"plan_id": plan_id})
    skill = assert_skill_governance_access(db, plan.skill_id, user)

    # 测试流来源的 plan 必须先 confirm 才能 materialize
    if req.entry_source and plan.entry_source and not plan.confirmed_at:
        raise_api_error(
            400,
            "sandbox.plan_not_confirmed",
            "该测试计划需先确认（confirm）后才能执行",
            {"plan_id": plan_id},
        )

    result = materialize_permission_case_plan(db, skill, user, plan)

    # 回写 plan 字段
    import datetime as _dt
    plan.latest_materialized_session_id = result["sandbox_session_id"]
    plan.last_used_at = _dt.datetime.utcnow()

    # 创建 run link
    from app.services.test_flow_history import create_run_link
    create_run_link(
        db,
        session_id=result["sandbox_session_id"],
        skill_id=plan.skill_id,
        plan_id=plan.id,
        plan_version=plan.plan_version,
        case_count=result["case_count"],
        created_by=user.id,
        entry_source=req.entry_source,
        decision_mode=req.decision_mode,
        conversation_id=req.conversation_id,
        workflow_id=req.workflow_id,
    )

    db.commit()

    # workflow event: 执行开始
    from app.services.test_flow_workflow import emit_test_flow_execution_started
    emit_test_flow_execution_started(
        db, skill_id=plan.skill_id, user_id=user.id,
        plan_id=plan.id, plan_version=plan.plan_version,
        sandbox_session_id=result["sandbox_session_id"],
        decision_mode=req.decision_mode,
    )

    return ok({
        "materialized_count": result["case_count"],
        "sandbox_session_id": result["sandbox_session_id"],
        "status": result["status"],
    })


@router.get("/{plan_id}/part2-review")
def get_sandbox_part2_review(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"plan_id": plan_id})
    assert_skill_governance_access(db, plan.skill_id, user)
    review = permission_contract_review_summary(db, plan.skill_id, plan_id)
    return ok(review)


@router.get("/{skill_id}/contract-review")
def get_sandbox_case_plan_contract_review(
    skill_id: int,
    plan_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    plan = latest_case_plan(db, skill_id) if plan_id is None else db.get(TestCasePlanDraft, plan_id)
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    plan_state = permission_case_plan_state(
        db,
        skill_id,
        plan=plan,
        declaration=declaration,
        bundle=bundle,
    )
    return ok({
        "skill_id": skill_id,
        "readiness": readiness,
        "plan": serialize_case_plan(plan),
        "plan_state": plan_state,
        "review": permission_contract_review_summary(db, skill_id, plan_id),
    })
