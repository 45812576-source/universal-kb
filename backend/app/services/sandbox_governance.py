"""Governance actions derived from interactive sandbox reports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.sandbox import SandboxTestReport
from app.models.sandbox import SandboxTestSession
from app.services.sandbox_remediation_agent import generate_remediation_plan
from app.services.preflight_governance import _create_staged_edit, _make_card
from app.services.skill_memo_service import sync_remediation_tasks
from app.services.studio_workflow_adapter import normalize_workflow_card, normalize_workflow_staged_edit

logger = logging.getLogger(__name__)


@dataclass
class SandboxGovernanceResult:
    cards: list[dict] = field(default_factory=list)
    staged_edits: list[dict] = field(default_factory=list)


_TARGET_KIND_LABELS = {
    "skill_prompt": "SKILL.md",
    "source_file": "附属文件",
    "tool_binding": "工具绑定",
    "knowledge_reference": "知识引用",
    "input_slot_definition": "输入槽位",
    "permission_config": "权限配置",
    "unknown": "Prompt 逻辑",
}


def _fallback_fix_items(part3: dict) -> list[dict]:
    """Build governance items for older reports without fix_plan_structured."""
    fix_plan = part3.get("fix_plan", []) or []
    top_issues = part3.get("top_issues", []) or []
    items: list[dict] = []
    for idx, fix in enumerate(fix_plan[:8]):
        issue = top_issues[idx] if idx < len(top_issues) and isinstance(top_issues[idx], dict) else {}
        fix_text = str(fix).strip()
        reason = str(issue.get("reason") or fix_text or "沙盒测试发现待整改项").strip()
        items.append({
            "id": f"legacy_fix_{idx + 1}",
            "title": f"修复: {reason[:80]}",
            "priority": "p1",
            "problem_ids": [str(issue.get("source") or idx + 1)],
            "action_type": "fix_prompt_logic",
            "target_kind": "skill_prompt",
            "target_ref": "SKILL.md",
            "suggested_changes": fix_text,
            "acceptance_rule": "重新运行沙盒测试后对应问题不再出现。",
            "retest_scope": [],
            "estimated_gain": "提升沙盒测试通过率",
        })
    return items


def _risk_level(priority: str | None) -> str:
    if priority == "p0":
        return "high"
    if priority == "p1":
        return "medium"
    return "low"


def _fix_plan_patch(item: dict) -> str:
    title = str(item.get("title", "修复沙盒测试问题")).strip()
    target_kind = str(item.get("target_kind", "unknown")).strip() or "unknown"
    target_ref = str(item.get("target_ref", "")).strip()
    suggested_changes = str(item.get("suggested_changes", "")).strip()
    acceptance_rule = str(item.get("acceptance_rule", "")).strip()
    retest_scope = item.get("retest_scope", []) or []
    lines = [
        "",
        "",
        f"## 沙盒测试整改要求：{title}",
        f"- 整改对象：{_TARGET_KIND_LABELS.get(target_kind, 'Prompt 逻辑')}" + (f"（{target_ref}）" if target_ref else ""),
    ]
    if suggested_changes:
        lines.append(f"- 具体修改：{suggested_changes}")
    if acceptance_rule:
        lines.append(f"- 验收标准：{acceptance_rule}")
    if retest_scope:
        lines.append(f"- 回归范围：{', '.join(str(scope) for scope in retest_scope[:5])}")
    lines.append("- 输出要求：先给结论，再给依据、边界和下一步动作。")
    return "\n".join(lines) + "\n"


def _target_for_item(item: dict) -> tuple[str, str | None]:
    target_kind = str(item.get("target_kind", "unknown")).strip() or "unknown"
    target_ref = str(item.get("target_ref", "")).strip()
    if target_kind == "source_file" and target_ref:
        return "source_file", target_ref
    return "system_prompt", None


def _default_staged_edit(
    db: Session,
    *,
    skill_id: int,
    item: dict,
) -> tuple[dict, dict]:
    target_type, target_key = _target_for_item(item)
    staged = _create_staged_edit(
        db,
        skill_id=skill_id,
        target_type=target_type,
        target_key=target_key,
        summary=str(item.get("title", "修复沙盒测试问题"))[:200],
        diff_ops=[{"op": "insert", "old": "", "new": _fix_plan_patch(item)}],
        risk_level=_risk_level(item.get("priority")),
    )
    card = _make_card(
        f"sandbox-report-{skill_id}-{item.get('id')}",
        str(item.get("title", "修复沙盒测试问题"))[:120],
        str(item.get("suggested_changes") or item.get("acceptance_rule") or "已根据沙盒报告生成一键整改建议。")[:300],
        reason=str(item.get("acceptance_rule") or item.get("estimated_gain") or "按沙盒报告要求修复后再回归测试。")[:300],
        staged_edit_id=int(staged["id"]),
    )
    card["content"]["problem_refs"] = item.get("problem_ids", [])
    card["content"]["target_kind"] = item.get("target_kind", "unknown")
    card["content"]["target_ref"] = item.get("target_ref", "")
    return staged, card


async def build_sandbox_report_governance(
    db: Session,
    *,
    skill_id: int,
    report: SandboxTestReport,
) -> SandboxGovernanceResult:
    part3 = report.part3_evaluation or {}
    issues = part3.get("issues", []) or []
    session = db.get(SandboxTestSession, report.session_id)

    cards: list[dict] = []
    staged_edits: list[dict] = []
    issue_map = {str(item.get("issue_id")): item for item in issues if item.get("issue_id")}
    fix_plan = part3.get("fix_plan_structured", []) or _fallback_fix_items(part3)
    items_for_followup = fix_plan

    agent_succeeded = False
    if part3.get("fix_plan_structured"):
        try:
            plan = await generate_remediation_plan(db, skill_id, report)
            if plan.cards and plan.staged_edits:
                agent_succeeded = True
                sync_remediation_tasks(
                    db,
                    skill_id=skill_id,
                    tasks=plan.tasks,
                    source_report_id=report.id,
                    user_id=session.tester_id if session else None,
                )
                cards.extend([
                    normalize_workflow_card(card, source_type="sandbox_remediation", phase="remediate")
                    for card in plan.cards
                ])
                staged_edits.extend([
                    normalize_workflow_staged_edit(edit, source_type="sandbox_remediation")
                    for edit in plan.staged_edits
                ])
                items_for_followup = plan.tasks or fix_plan
        except Exception:
            logger.warning("Remediation agent failed for skill=%s report=%s, falling back", skill_id, report.id, exc_info=True)

    if not agent_succeeded:
        sync_remediation_tasks(
            db,
            skill_id=skill_id,
            tasks=fix_plan,
            source_report_id=report.id,
            user_id=session.tester_id if session else None,
        )
        for item in fix_plan[:8]:
            staged, card = _default_staged_edit(db, skill_id=skill_id, item=item)
            staged_edits.append(normalize_workflow_staged_edit(staged, source_type="sandbox_remediation"))
            cards.append(normalize_workflow_card(card, source_type="sandbox_remediation", phase="remediate"))

    for item in items_for_followup[:8]:
        target_kind = str(item.get("target_kind", "unknown"))
        if target_kind == "tool_binding":
            tool_ids = [
                int(item.get("tool_id"))
                for item in (session.tool_review if session else []) or []
                if item.get("confirmed") and item.get("tool_id")
            ]
            cards.append(normalize_workflow_card(_make_card(
                f"sandbox-report-tools-{skill_id}-{item.get('id')}",
                f"补充工具治理：{str(item.get('title', '工具整改'))[:80]}",
                "将沙盒确认过的工具绑定回当前 Skill，并刷新整改状态。",
                card_type="followup_prompt",
                reason=str(item.get("suggested_changes", "工具链路存在整改项"))[:300],
                preflight_action="bind_sandbox_tools",
                action_payload={
                    "source_report_id": report.id,
                    "problem_ids": item.get("problem_ids", []),
                    "tool_ids": sorted(set(tool_ids)),
                },
            ), source_type="sandbox_remediation", phase="remediate"))
        elif target_kind == "permission_config":
            table_names = [
                str(snap.get("table_name"))
                for snap in (session.permission_snapshot if session else []) or []
                if snap.get("confirmed") and snap.get("included_in_test") and snap.get("table_name")
            ]
            cards.append(normalize_workflow_card(_make_card(
                f"sandbox-report-data-{skill_id}-{item.get('id')}",
                f"补充数据权限绑定：{str(item.get('title', '数据整改'))[:80]}",
                "将沙盒确认通过的数据表写入 Skill 数据查询与运行绑定，避免整改只停留在 prompt 说明。",
                card_type="followup_prompt",
                reason=str(item.get("suggested_changes", "数据权限/数据源配置存在整改项"))[:300],
                preflight_action="bind_permission_tables",
                action_payload={
                    "source_report_id": report.id,
                    "problem_ids": item.get("problem_ids", []),
                    "table_names": sorted(set(table_names)),
                },
            ), source_type="sandbox_remediation", phase="remediate"))
        elif target_kind == "knowledge_reference":
            knowledge_ids = []
            for problem_id in item.get("problem_ids", []) or []:
                issue = issue_map.get(str(problem_id)) or {}
                for snippet in issue.get("evidence_snippets", []) or []:
                    if isinstance(snippet, str) and "knowledge_entry:" in snippet:
                        try:
                            knowledge_ids.append(int(snippet.rsplit("knowledge_entry:", 1)[-1]))
                        except Exception:
                            pass
            cards.append(normalize_workflow_card(_make_card(
                f"sandbox-report-knowledge-{skill_id}-{item.get('id')}",
                f"核对知识引用：{str(item.get('title', '知识整改'))[:80]}",
                "将沙盒证据中的知识引用写入 Skill 知识引用快照，并触发后续回归。",
                card_type="followup_prompt",
                reason=str(item.get("suggested_changes", "知识引用存在整改项"))[:300],
                preflight_action="bind_knowledge_references",
                action_payload={
                    "knowledge_ids": sorted(set(knowledge_ids)),
                    "source_report_id": report.id,
                },
            ), source_type="sandbox_remediation", phase="remediate"))

    db.commit()
    return SandboxGovernanceResult(cards=cards, staged_edits=staged_edits)
