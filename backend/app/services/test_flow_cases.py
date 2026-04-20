"""测试流 Case Plan 操作 — fork / confirm / summary。"""
import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.api_envelope import raise_api_error
from app.models.skill_governance import TestCaseDraft, TestCasePlanDraft


def build_plan_summary(plan: TestCasePlanDraft) -> dict[str, Any]:
    """从 plan 生成摘要卡数据。"""
    return {
        "plan_id": plan.id,
        "skill_id": plan.skill_id,
        "plan_version": plan.plan_version,
        "status": plan.status,
        "case_count": plan.case_count,
        "focus_mode": plan.focus_mode,
        "source_plan_id": plan.source_plan_id,
        "generation_mode": plan.generation_mode,
    }


def fork_case_plan(
    db: Session,
    plan_id: int,
    mode: str,
    user_id: int,
    entry_source: str | None = None,
    conversation_id: int | None = None,
) -> TestCasePlanDraft:
    """基于现有 plan fork 新版本并复制 cases。

    mode: generate | reuse | revise | regenerate
    """
    source = db.get(TestCasePlanDraft, plan_id)
    if not source:
        raise_api_error(404, "test_flow.plan_not_found", "Plan not found", {"plan_id": plan_id})

    # 取当前 skill 最大 plan_version
    max_version = (
        db.query(TestCasePlanDraft.plan_version)
        .filter(TestCasePlanDraft.skill_id == source.skill_id)
        .order_by(TestCasePlanDraft.plan_version.desc())
        .first()
    )
    next_version = (max_version[0] + 1) if max_version else 1

    new_plan = TestCasePlanDraft(
        skill_id=source.skill_id,
        workspace_id=source.workspace_id,
        bundle_id=source.bundle_id,
        declaration_id=source.declaration_id,
        plan_version=next_version,
        skill_content_version=source.skill_content_version,
        governance_version=source.governance_version,
        permission_declaration_version=source.permission_declaration_version,
        status="generated",
        focus_mode=source.focus_mode,
        max_cases=source.max_cases,
        case_count=0,
        blocking_issues_json=source.blocking_issues_json,
        source_plan_id=source.id,
        generation_mode=mode,
        entry_source=entry_source,
        conversation_id=conversation_id,
        created_by=user_id,
    )
    db.add(new_plan)
    db.flush()

    # 复制 cases（仅非 discarded）
    source_cases = (
        db.query(TestCaseDraft)
        .filter(TestCaseDraft.plan_id == source.id)
        .filter(TestCaseDraft.status != "discarded")
        .order_by(TestCaseDraft.id)
        .all()
    )
    count = 0
    for idx, src_case in enumerate(source_cases):
        new_case = TestCaseDraft(
            plan_id=new_plan.id,
            skill_id=src_case.skill_id,
            target_role_ref=src_case.target_role_ref,
            role_label=src_case.role_label,
            asset_ref=src_case.asset_ref,
            asset_name=src_case.asset_name,
            asset_type=src_case.asset_type,
            case_type=src_case.case_type,
            risk_tags_json=src_case.risk_tags_json,
            prompt=src_case.prompt,
            expected_behavior=src_case.expected_behavior,
            source_refs_json=src_case.source_refs_json,
            source_verification_status=src_case.source_verification_status,
            data_source_policy=src_case.data_source_policy,
            status="generated",
            granular_refs_json=src_case.granular_refs_json,
            controlled_fields_json=src_case.controlled_fields_json,
            created_at=datetime.datetime.utcnow(),
        )
        db.add(new_case)
        count += 1

    new_plan.case_count = count
    new_plan.summary_json = build_plan_summary(new_plan)
    db.flush()
    return new_plan


def confirm_case_plan(db: Session, plan_id: int) -> dict[str, Any]:
    """用户确认 plan 可执行 — 设置 confirmed_at + status。"""
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan:
        raise_api_error(404, "test_flow.plan_not_found", "Plan not found", {"plan_id": plan_id})

    now = datetime.datetime.utcnow()
    plan.confirmed_at = now
    if plan.status == "generated":
        plan.status = "confirmed"
    plan.summary_json = build_plan_summary(plan)
    db.flush()

    return {
        "plan_id": plan.id,
        "status": plan.status,
        "confirmed_at": now.isoformat(),
    }
