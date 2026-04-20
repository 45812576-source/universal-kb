"""测试流就绪性检查 — 委托 skill_governance_service。"""
from typing import Any

from sqlalchemy.orm import Session

from app.services.skill_governance_service import (
    latest_bundle,
    latest_declaration,
    ensure_permission_declaration_prompt_sync,
    permission_case_plan_readiness,
)


def check_readiness(db: Session, skill_id: int) -> dict[str, Any]:
    """检查 Skill 是否满足生成测试用例的前提条件，返回 readiness + mount_cta。"""
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)

    mount_cta = None
    if not readiness["ready"]:
        issues = readiness.get("blocking_issues", [])
        if "missing_confirmed_declaration" in issues:
            mount_cta = "complete_permission_declaration"
        elif "missing_bound_assets" in issues:
            mount_cta = "mount_data_assets"
        else:
            mount_cta = "resolve_blocking_issues"

    return {
        **readiness,
        "mount_cta": mount_cta,
    }
