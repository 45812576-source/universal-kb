"""测试流就绪性检查 — 委托 skill_governance_service。"""
from typing import Any

from sqlalchemy.orm import Session

from app.services.skill_governance_service import (
    latest_bundle,
    latest_declaration,
    ensure_permission_declaration_prompt_sync,
    permission_case_plan_readiness,
)

# ── 门禁原因映射 ─────────────────────────────────────────────────────────────

_GATE_REASON_MAP: dict[str, dict[str, Any]] = {
    "missing_bound_assets": {
        "title": "未绑定可测试数据资产",
        "detail": "Skill 未绑定任何数据表，无法生成测试用例。请先在治理面板绑定数据资产。",
        "severity": "critical",
        "step_id": "bind_assets",
        "action": "go_bound_assets",
        "order": 1,
    },
    "missing_confirmed_declaration": {
        "title": "未确认权限声明",
        "detail": "权限声明尚未生成或确认。请先生成并采纳权限声明。",
        "severity": "critical",
        "step_id": "confirm_declaration",
        "action": "generate_declaration",
        "order": 2,
    },
    "missing_skill_data_grant": {
        "title": "数据表授权未配置",
        "detail": "Skill 的数据表授权尚未配置，请在治理面板补齐。",
        "severity": "critical",
        "step_id": "complete_table_governance",
        "action": "go_readiness",
        "order": 3,
    },
    "grant_missing_view_binding": {
        "title": "数据表视图未绑定",
        "detail": "数据表视图尚未绑定到 Skill，请在治理面板补齐。",
        "severity": "critical",
        "step_id": "complete_table_governance",
        "action": "go_readiness",
        "order": 4,
    },
    "missing_role_group_binding": {
        "title": "角色组未绑定",
        "detail": "Skill 的角色组尚未绑定，请在治理面板补齐。",
        "severity": "critical",
        "step_id": "complete_table_governance",
        "action": "go_readiness",
        "order": 5,
    },
    "missing_table_permission_policy": {
        "title": "表权限策略未配置",
        "detail": "数据表的权限策略尚未配置，请在治理面板补齐。",
        "severity": "critical",
        "step_id": "complete_table_governance",
        "action": "go_readiness",
        "order": 6,
    },
    "skill_content_version_mismatch": {
        "title": "Skill 内容版本已变化",
        "detail": "Skill 内容在上次治理之后发生了变化，需要刷新治理状态。",
        "severity": "warning",
        "step_id": "refresh_governance",
        "action": "refresh_governance",
        "order": 7,
    },
    "governance_version_mismatch": {
        "title": "治理版本已变化",
        "detail": "治理版本与当前状态不一致，需要刷新。",
        "severity": "warning",
        "step_id": "refresh_governance",
        "action": "refresh_governance",
        "order": 8,
    },
    "stale_governance_bundle": {
        "title": "治理包已过期",
        "detail": "治理包版本已过期，需要刷新治理状态。",
        "severity": "warning",
        "step_id": "refresh_governance",
        "action": "refresh_governance",
        "order": 9,
    },
}

# step_id → guided step 信息
_GUIDED_STEP_DEFS: dict[str, dict[str, Any]] = {
    "bind_assets": {
        "order": 1,
        "title": "绑定数据资产",
        "detail": "在治理面板中为 Skill 绑定需要测试的数据表。",
        "action": "go_bound_assets",
        "action_label": "去绑定",
    },
    "confirm_declaration": {
        "order": 2,
        "title": "生成并确认权限声明",
        "detail": "生成权限声明文本并采纳挂载到 Skill。",
        "action": "generate_declaration",
        "action_label": "生成声明",
    },
    "complete_table_governance": {
        "order": 3,
        "title": "补齐表治理配置",
        "detail": "完成数据表授权、视图绑定、角色组绑定、权限策略等配置。",
        "action": "go_readiness",
        "action_label": "去配置",
    },
    "refresh_governance": {
        "order": 4,
        "title": "刷新治理状态",
        "detail": "Skill 内容或治理版本发生变化，需要刷新以同步最新状态。",
        "action": "refresh_governance",
        "action_label": "刷新",
    },
}


def _build_case_generation_gate_details(readiness: dict[str, Any]) -> dict[str, Any]:
    """基于 readiness 构建门禁语义字段。"""
    issues = readiness.get("blocking_issues", [])

    gate_reasons: list[dict[str, Any]] = []
    seen_step_ids: set[str] = set()
    for code in issues:
        info = _GATE_REASON_MAP.get(code)
        if not info:
            gate_reasons.append({
                "code": code,
                "title": code.replace("_", " ").title(),
                "detail": f"阻断原因：{code}",
                "severity": "warning",
                "step_id": "unknown",
                "action": "go_readiness",
            })
            seen_step_ids.add("unknown")
            continue
        gate_reasons.append({
            "code": code,
            "title": info["title"],
            "detail": info["detail"],
            "severity": info["severity"],
            "step_id": info["step_id"],
            "action": info["action"],
        })
        seen_step_ids.add(info["step_id"])

    # 按优先级排序
    gate_reasons.sort(key=lambda r: _GATE_REASON_MAP.get(r["code"], {}).get("order", 99))

    # 构建 guided_steps：blocked 之前为 done，第一个 blocked 高亮，之后全部 todo
    raw_steps: list[dict[str, Any]] = []
    for step_id, step_def in sorted(_GUIDED_STEP_DEFS.items(), key=lambda x: x[1]["order"]):
        is_blocked = step_id in seen_step_ids
        raw_steps.append({
            "id": step_id,
            "order": step_def["order"],
            "title": step_def["title"],
            "detail": step_def["detail"],
            "is_blocked": is_blocked,
            "action": step_def["action"],
            "action_label": step_def["action_label"],
        })

    guided_steps: list[dict[str, Any]] = []
    first_blocked_seen = False
    for step in raw_steps:
        is_blocked = step.pop("is_blocked")
        if not first_blocked_seen:
            if is_blocked:
                step["status"] = "blocked"
                first_blocked_seen = True
            else:
                step["status"] = "done"
        else:
            # 第一个 blocked 之后：即使该步骤本身也 blocked，也标 todo（只高亮第一个）
            step["status"] = "todo"
        guided_steps.append(step)

    primary_action = gate_reasons[0]["action"] if gate_reasons else None

    reason_titles = "、".join(r["title"] for r in gate_reasons[:3])
    gate_summary = f"需要先完成：{reason_titles}" if reason_titles else "前置条件未满足"

    return {
        "blocked_stage": "case_generation_gate",
        "blocked_before": "case_generation",
        "case_generation_allowed": False,
        "quality_evaluation_started": False,
        "verdict_label": "尚未开始质量检测",
        "verdict_reason": f"前置条件未完成：{reason_titles}" if reason_titles else "前置条件未完成",
        "gate_summary": gate_summary,
        "gate_reasons": gate_reasons,
        "guided_steps": guided_steps,
        "primary_action": primary_action,
    }


def check_readiness(db: Session, skill_id: int) -> dict[str, Any]:
    """检查 Skill 是否满足生成测试用例的前提条件，返回 readiness + mount_cta + 门禁语义。"""
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

    result = {
        **readiness,
        "mount_cta": mount_cta,
    }

    if not readiness["ready"]:
        result.update(_build_case_generation_gate_details(readiness))
    else:
        result.update({
            "blocked_stage": None,
            "blocked_before": None,
            "case_generation_allowed": True,
            "quality_evaluation_started": False,
            "verdict_label": "可生成测试用例",
            "verdict_reason": None,
            "gate_summary": None,
            "gate_reasons": [],
            "guided_steps": [],
            "primary_action": None,
        })

    return result
