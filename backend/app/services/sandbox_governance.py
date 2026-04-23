"""Governance actions derived from interactive sandbox reports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.sandbox import SandboxTestReport
from app.models.sandbox import SandboxTestSession
from app.services.governance_action_compiler import (
    TARGET_KIND_LABELS,
    build_followup_card,
    normalize_evidence_snippets,
    string_list,
)
from app.services.sandbox_remediation_agent import generate_remediation_plan
from app.services.skill_memo_service import sync_remediation_tasks
from app.services.studio_workflow_adapter import normalize_workflow_card, normalize_workflow_staged_edit

logger = logging.getLogger(__name__)


@dataclass
class SandboxGovernanceResult:
    cards: list[dict] = field(default_factory=list)
    staged_edits: list[dict] = field(default_factory=list)


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


def _issue_evidence(item: dict, issue_map: dict[str, dict]) -> list[str]:
    evidence: list[str] = []
    for problem_id in string_list(item.get("problem_ids")):
        issue = issue_map.get(problem_id) or {}
        evidence.extend(normalize_evidence_snippets(issue.get("evidence_snippets", [])))
    return evidence[:5]


def _build_action_payload(
    item: dict,
    *,
    report: SandboxTestReport,
    session: SandboxTestSession | None,
    issue_map: dict[str, dict],
) -> tuple[str | None, dict]:
    target_kind = str(item.get("target_kind", "unknown"))
    base_payload = {
        "source_report_id": report.id,
        "task_id": item.get("id") or item.get("task_id"),
        "problem_ids": item.get("problem_ids", []),
        "target_kind": target_kind,
        "target_ref": item.get("target_ref", ""),
        "acceptance_rule": item.get("acceptance_rule", ""),
        "retest_scope": item.get("retest_scope", []),
        "evidence_snippets": _issue_evidence(item, issue_map),
    }

    if target_kind == "tool_binding":
        tool_ids = [
            int(review.get("tool_id"))
            for review in (session.tool_review if session else []) or []
            if review.get("confirmed") and review.get("tool_id")
        ]
        return "bind_sandbox_tools", {**base_payload, "tool_ids": sorted(set(tool_ids))}

    if target_kind == "permission_config":
        table_names = [
            str(snap.get("table_name"))
            for snap in (session.permission_snapshot if session else []) or []
            if snap.get("confirmed") and snap.get("included_in_test") and snap.get("table_name")
        ]
        return "bind_permission_tables", {**base_payload, "table_names": sorted(set(table_names))}

    if target_kind == "knowledge_reference":
        knowledge_ids = []
        for snippet in base_payload["evidence_snippets"]:
            if "knowledge_entry:" in snippet:
                try:
                    knowledge_ids.append(int(snippet.rsplit("knowledge_entry:", 1)[-1]))
                except Exception:
                    pass
        return "bind_knowledge_references", {**base_payload, "knowledge_ids": sorted(set(knowledge_ids))}

    return None, base_payload


def _actionable_task_card(
    skill_id: int,
    item: dict,
    *,
    report: SandboxTestReport,
    session: SandboxTestSession | None,
    issue_map: dict[str, dict],
) -> dict:
    preflight_action, action_payload = _build_action_payload(
        item,
        report=report,
        session=session,
        issue_map=issue_map,
    )
    target_kind = str(item.get("target_kind", "unknown"))
    card = build_followup_card(
        card_id=f"sandbox-report-{skill_id}-{item.get('id')}",
        title=str(item.get("title", "修复沙盒测试问题")),
        target_kind=target_kind,
        target_ref=str(item.get("target_ref", "")),
        problem_refs=string_list(item.get("problem_ids")),
        reason=str(item.get("acceptance_rule") or item.get("estimated_gain") or "按沙盒报告要求修复后再回归测试。")[:300],
        acceptance_rule=str(item.get("acceptance_rule", "")),
        evidence_snippets=_issue_evidence(item, issue_map),
        suggested_changes=str(item.get("suggested_changes") or ""),
        expected_deliverable=str(item.get("suggested_changes") or item.get("acceptance_rule") or ""),
        preflight_action=preflight_action,
        action_payload=action_payload,
        extra_content={"retest_scope": item.get("retest_scope", [])},
    )
    return card


def _task_identity(item: dict) -> str:
    return str(item.get("task_id") or item.get("id") or "")


def _covered_task_ids(cards: list[dict]) -> set[str]:
    covered: set[str] = set()
    for card in cards:
        content = card.get("content") if isinstance(card.get("content"), dict) else {}
        task_id = str(content.get("task_id") or "").strip()
        if task_id:
            covered.add(task_id)
    return covered


def _append_actionable_task_cards(
    cards: list[dict],
    *,
    skill_id: int,
    report: SandboxTestReport,
    session: SandboxTestSession | None,
    issue_map: dict[str, dict],
    tasks: list[dict],
) -> None:
    covered = _covered_task_ids(cards)
    positional_covered = len(cards) if cards else 0
    for index, item in enumerate(tasks[:8]):
        if index < positional_covered:
            continue
        task_id = _task_identity(item)
        if task_id and task_id in covered:
            continue
        cards.append(normalize_workflow_card(
            _actionable_task_card(
                skill_id,
                item,
                report=report,
                session=session,
                issue_map=issue_map,
            ),
            source_type="sandbox_remediation",
            phase="remediate",
        ))


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

    agent_tasks: list[dict] = []
    if part3.get("fix_plan_structured"):
        try:
            plan = await generate_remediation_plan(db, skill_id, report)
            if plan.tasks:
                agent_tasks = plan.tasks
                sync_remediation_tasks(
                    db,
                    skill_id=skill_id,
                    tasks=plan.tasks,
                    source_report_id=report.id,
                    user_id=session.tester_id if session else None,
                )
                items_for_followup = plan.tasks
            if plan.cards:
                cards.extend([
                    normalize_workflow_card(card, source_type="sandbox_remediation", phase="remediate")
                    for card in plan.cards
                ])
            if plan.staged_edits:
                staged_edits.extend([
                    normalize_workflow_staged_edit(edit, source_type="sandbox_remediation")
                    for edit in plan.staged_edits
                ])
        except Exception:
            logger.warning("Remediation agent failed for skill=%s report=%s, falling back", skill_id, report.id, exc_info=True)

    if not agent_tasks:
        sync_remediation_tasks(
            db,
            skill_id=skill_id,
            tasks=fix_plan,
            source_report_id=report.id,
            user_id=session.tester_id if session else None,
        )

    _append_actionable_task_cards(
        cards,
        skill_id=skill_id,
        report=report,
        session=session,
        issue_map=issue_map,
        tasks=items_for_followup,
    )

    db.commit()
    return SandboxGovernanceResult(cards=cards, staged_edits=staged_edits)
