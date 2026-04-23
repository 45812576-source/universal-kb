"""Shared compiler for actionable governance follow-up cards."""

from __future__ import annotations

from typing import Any


TARGET_KIND_LABELS = {
    "skill_prompt": "SKILL.md",
    "source_file": "附属文件",
    "tool_binding": "工具绑定",
    "knowledge_reference": "知识引用",
    "input_slot_definition": "输入槽位",
    "permission_config": "权限配置",
    "skill_metadata": "Skill 元数据",
    "unknown": "Prompt 逻辑",
}

AUTO_ACTIONS = {
    "open_skill_editor",
    "confirm_archive",
    "reindex_knowledge",
    "navigate_tools",
    "navigate_data_assets",
    "bind_sandbox_tools",
    "bind_knowledge_references",
    "bind_permission_tables",
    "binding_action",
}


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_evidence_snippets(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()][:5]


def default_immediate_steps(
    *,
    target_kind: str,
    target_ref: str,
    suggested_changes: str,
    acceptance_rule: str,
) -> list[str]:
    target_label = TARGET_KIND_LABELS.get(target_kind, TARGET_KIND_LABELS["unknown"])
    target_display = target_ref or target_label
    suggested = suggested_changes.strip() or "按整改建议补齐缺失内容"
    acceptance = acceptance_rule.strip() or "重新运行对应验证后问题不再出现。"

    if target_kind in {"skill_prompt", "source_file", "input_slot_definition", "skill_metadata", "unknown"}:
        return [
            f"打开 {target_display}，定位本卡片对应的问题段落。",
            f"按整改要求落地：{suggested}",
            f"保存后按验收标准自查：{acceptance}",
            "仅重跑本卡片关联问题对应的用例，确认问题消失。",
        ]
    if target_kind == "knowledge_reference":
        return [
            "核对证据中的知识条目与当前 Skill 绑定关系。",
            "将已验证知识写入 Skill 知识引用快照。",
            f"按验收标准复查：{acceptance}",
            "重跑关联用例确认回答引用命中已绑定知识。",
        ]
    if target_kind == "permission_config":
        return [
            "核对已确认的数据表与权限快照。",
            "将数据表写入 Skill 数据查询和运行绑定。",
            f"按验收标准复查：{acceptance}",
            "重跑关联用例确认数据来源可用且覆盖必填字段。",
        ]
    if target_kind == "tool_binding":
        return [
            "核对已确认的工具清单。",
            "将已确认工具绑定回当前 Skill。",
            f"按验收标准复查：{acceptance}",
            "重跑关联用例确认工具调用链路可用。",
        ]
    return [
        f"按整改要求落地：{suggested}",
        f"按验收标准自查：{acceptance}",
        "重跑关联用例确认问题消失。",
    ]


def actionable_summary(*, immediate_steps: list[str], expected_deliverable: str) -> str:
    lead = immediate_steps[0] if immediate_steps else "按卡片要求立即处理。"
    deliverable = expected_deliverable.strip() or "完成该整改项并回归验证。"
    return f"立即执行：{lead} 交付物：{deliverable}"[:300]


def default_followup_actions(
    *,
    preflight_action: str | None,
    target_kind: str,
    target_ref: str,
) -> list[dict[str, str]]:
    if preflight_action and preflight_action in AUTO_ACTIONS:
        return [{"label": "一键处理", "type": "adopt"}, {"label": "忽略", "type": "reject"}]
    if target_ref or target_kind == "skill_prompt":
        return [{"label": "打开目标", "type": "view_diff"}, {"label": "继续细化", "type": "refine"}, {"label": "忽略", "type": "reject"}]
    return [{"label": "继续细化", "type": "refine"}, {"label": "忽略", "type": "reject"}]


def build_followup_card(
    *,
    card_id: str,
    title: str,
    target_kind: str = "unknown",
    target_ref: str = "",
    problem_refs: list[str] | None = None,
    reason: str = "",
    acceptance_rule: str = "",
    evidence_snippets: list[str] | None = None,
    suggested_changes: str = "",
    expected_deliverable: str = "",
    immediate_steps: list[str] | None = None,
    preflight_action: str | None = None,
    action_payload: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    status: str = "pending",
    extra_content: dict[str, Any] | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_kind = str(target_kind or "unknown")
    normalized_ref = str(target_ref or "")
    normalized_problem_refs = string_list(problem_refs or [])
    normalized_acceptance = str(acceptance_rule or "")
    normalized_evidence = normalize_evidence_snippets(evidence_snippets or [])
    deliverable = str(expected_deliverable or suggested_changes or normalized_acceptance or "").strip()
    steps = immediate_steps or default_immediate_steps(
        target_kind=normalized_kind,
        target_ref=normalized_ref,
        suggested_changes=str(suggested_changes or deliverable),
        acceptance_rule=normalized_acceptance,
    )
    payload = {
        **(action_payload or {}),
        "problem_ids": normalized_problem_refs,
        "target_kind": normalized_kind,
        "target_ref": normalized_ref,
        "acceptance_rule": normalized_acceptance,
        "immediate_steps": steps,
        "expected_deliverable": deliverable,
        "evidence_snippets": normalized_evidence,
    }
    content: dict[str, Any] = {
        "summary": actionable_summary(immediate_steps=steps, expected_deliverable=deliverable),
        "reason": reason,
        "problem_refs": normalized_problem_refs,
        "target_kind": normalized_kind,
        "target_ref": normalized_ref,
        "acceptance_rule": normalized_acceptance,
        "evidence_snippets": normalized_evidence,
        "immediate_steps": steps,
        "expected_deliverable": deliverable,
        "action_payload": payload,
    }
    if preflight_action:
        content["preflight_action"] = preflight_action
    if extra_content:
        content.update(extra_content)
    card = {
        "id": card_id,
        "type": "followup_prompt",
        "title": title[:120],
        "content": content,
        "status": status,
        "actions": actions or default_followup_actions(
            preflight_action=preflight_action,
            target_kind=normalized_kind,
            target_ref=normalized_ref,
        ),
    }
    if extra_fields:
        card.update(extra_fields)
    return card
