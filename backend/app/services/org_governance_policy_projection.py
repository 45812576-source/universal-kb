from __future__ import annotations

from typing import Any


SENSITIVE_KEYWORDS = (
    "薪酬", "绩效", "客户", "合同", "价格", "利润", "预算", "财务", "审批意见",
    "审计", "权限配置", "安全", "风控", "组织调整", "裁撤", "离职", "HR", "人事",
)
ACTION_KEYWORDS = {
    "view": ("查看", "读取", "参考", "浏览", "可见"),
    "edit": ("编辑", "维护", "更新", "录入"),
    "review": ("审核", "复核", "校验", "确认"),
    "approve": ("审批", "通过", "驳回", "批准"),
    "publish": ("发布", "生效", "上线", "归档"),
    "export": ("导出", "下载", "对外发送"),
    "manage": ("配置", "分配", "授权", "管理"),
    "execute": ("调用", "运行", "执行"),
    "delete": ("删除", "回滚", "撤销"),
}
REVIEW_ACTIONS = {"export", "publish", "manage", "grant", "approve"}


def _text_from_structured(structured_by_tab: dict[str, Any]) -> str:
    chunks: list[str] = []
    for tab_key, payload in (structured_by_tab or {}).items():
        if not isinstance(payload, dict):
            continue
        tab_payload = payload.get(tab_key) or {}
        if isinstance(tab_payload, dict):
            chunks.extend(str(tab_payload.get(key) or "") for key in (
                "facts_text",
                "governance_semantics_text",
                "analysis_text",
                "actions_text",
            ))
    return "\n".join(chunks)


def _detect_actions(text: str) -> list[str]:
    actions: list[str] = []
    for action, keywords in ACTION_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            actions.append(action)
    return actions or ["view"]


def _risk_status(action: str, text: str, conflicts: list[dict[str, Any]], sod_risks: list[dict[str, Any]]) -> tuple[str, bool]:
    sensitive = any(keyword in text for keyword in SENSITIVE_KEYWORDS)
    cross_department = "跨部门" in text or "部门共享" in text
    raw_access = "原文" in text or "raw" in text
    if conflicts or any((risk.get("severity") == "high") for risk in sod_risks):
        return "blocked", True
    if action in REVIEW_ACTIONS or sensitive or cross_department or raw_access:
        return "needs_review", True
    return "auto_apply_candidate", False


def detect_separation_of_duty_risks(structured_by_tab: dict[str, Any]) -> list[dict[str, Any]]:
    text = _text_from_structured(structured_by_tab)
    risks: list[dict[str, Any]] = []
    checks = [
        ("create_approve_conflict", ("创建", "审批"), "同一主体可能同时创建和审批治理产物。"),
        ("maintain_audit_conflict", ("维护", "审计"), "同一主体可能同时维护和审计规则。"),
        ("raw_redact_publish_conflict", ("原文", "脱敏", "发布"), "同一主体可能覆盖原文查看、脱敏和发布。"),
        ("grant_use_conflict", ("授权", "使用"), "授权人与使用人边界不清。"),
        ("delete_audit_conflict", ("删除", "审计"), "删除动作可能影响审计证据保全。"),
    ]
    for risk_type, keywords, reason in checks:
        if all(keyword in text for keyword in keywords):
            risks.append({
                "risk_type": risk_type,
                "subject": "待确认主体",
                "conflicting_actions": list(keywords),
                "resource": "组织治理快照",
                "severity": "high" if risk_type in {"grant_use_conflict", "delete_audit_conflict"} else "medium",
                "reason": reason,
                "recommended_control": "引入双人复核或拆分审批 / 审计角色。",
                "evidence_refs": [],
            })
    return risks


def project_governance_outputs(
    structured_by_tab: dict[str, Any],
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    conflicts = conflicts or []
    text = _text_from_structured(structured_by_tab)
    actions = _detect_actions(text)
    sod_risks = detect_separation_of_duty_risks(structured_by_tab)
    subject = {"type": "role", "name": "待确认治理角色", "id": None}
    resource = {"type": "governance_snapshot", "name": "组织治理快照", "id": None}

    authority_map: list[dict[str, Any]] = []
    resource_access_matrix: list[dict[str, Any]] = []
    approval_route_candidates: list[dict[str, Any]] = []
    policy_hints: list[dict[str, Any]] = []

    for action in actions:
        status, approval_required = _risk_status(action, text, conflicts, sod_risks)
        evidence_level = "derived" if status != "blocked" else "assumed"
        confidence = 0.72 if evidence_level == "derived" else 0.35
        authority_map.append({
            "subject": subject,
            "authority": {
                "action": action,
                "resource": resource,
                "scope": {
                    "visibility": "department" if "部门" in text else "needs_confirmation",
                    "sharing": "summary" if "摘要" in text or "脱敏" in text else "needs_confirmation",
                    "time": None,
                    "scenario": ["daily_operation"],
                },
                "source": {
                    "type": "derived_from_markdown",
                    "evidence_level": evidence_level,
                    "evidence_refs": [],
                },
                "confidence": confidence,
                "requires_human_confirmation": approval_required,
            },
        })
        resource_access_matrix.append({
            "subject_type": "role",
            "subject_name": subject["name"],
            "resource_type": resource["type"],
            "resource_name": resource["name"],
            "actions": [action],
            "conditions": ["人工确认后生效"] if approval_required else [],
            "visibility_scope": "department" if "部门" in text else "needs_confirmation",
            "redaction_mode": "summary" if "摘要" in text else ("masked" if "脱敏" in text else "deny" if status == "blocked" else "summary"),
            "approval_required": approval_required,
            "approver_source": "resource_owner" if approval_required else None,
            "evidence_level": evidence_level,
            "confidence": confidence,
            "status": status,
        })
        if approval_required:
            approval_route_candidates.append({
                "action": action,
                "resource": resource,
                "requester_subject": subject,
                "recommended_route": [
                    {"approver_type": "resource_owner", "approver_name": None, "required": True},
                    {"approver_type": "governance_admin", "approver_name": None, "required": action in REVIEW_ACTIONS},
                ],
                "trigger_condition": "高风险动作、敏感资源、跨部门范围或证据不足时触发。",
                "risk_level": "high" if status == "blocked" else "medium",
                "evidence_refs": [],
                "confidence": confidence,
                "status": "blocked" if status == "blocked" else "needs_confirmation",
            })
        policy_hints.append({
            "policy_type": "require_approval" if approval_required else "allow",
            "subject": subject,
            "resource": resource,
            "actions": [action],
            "conditions": ["禁止自动应用假设性、跨部门原文、高敏原文、导出或 SoD 高风险权限。"],
            "redaction": "summary",
            "rationale": "从治理快照 Markdown 的事实区和治理语义区派生，首版仅作为候选策略。",
            "evidence_level": evidence_level,
            "confidence": confidence,
            "status": status,
            "blocking_reasons": ["存在冲突或职责分离风险"] if status == "blocked" else [],
        })

    return {
        "authority_map": authority_map,
        "resource_access_matrix": resource_access_matrix,
        "approval_route_candidates": approval_route_candidates,
        "policy_hints": policy_hints,
        "governance_questions": [],
        "separation_of_duty_risks": sod_risks,
    }
