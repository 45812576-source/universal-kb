"""沙盒测试报告生成 + 知识库持久化。

报告四层结构：
  Part 1 — Q1/Q2/Q3 检测结果（含证据化审批字段）
  Part 2 — 权限穷尽测试用例矩阵（含评分细项）
  Part 3 — 质量/易用性/反幻觉三项评价 + 结构化问题清单 + 结构化 fix_plan + 重测建议

报告不可变，生成后写入知识库，不覆盖旧报告。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models.sandbox import (
    SandboxTestSession,
    SandboxTestCase,
    SandboxTestReport,
    CaseVerdict,
)
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.services.sandbox_quality_standard import QUALITY_DIMENSIONS, QUALITY_PASS_THRESHOLD

logger = logging.getLogger(__name__)


# ── target_kind 推断 ────────────────────────────────────────────────────────

_TARGET_KIND_PATTERNS = [
    (re.compile(r"(prompt|指令|系统提示|system.?prompt|提示词)", re.I), "skill_prompt", "SKILL.md"),
    (re.compile(r"(知识|引用|reference|knowledge|RAG|检索)", re.I), "knowledge_reference", None),
    (re.compile(r"(工具|调用|tool|API|函数)", re.I), "tool_binding", None),
    (re.compile(r"(输入|slot|参数|input)", re.I), "input_slot_definition", None),
    (re.compile(r"(权限|permission|脱敏|mask|可见)", re.I), "permission_config", None),
]


def _infer_target_kind(reason: str) -> tuple[str, str | None]:
    """从扣分原因推断整改目标类型和引用。"""
    for pattern, kind, ref in _TARGET_KIND_PATTERNS:
        if pattern.search(reason):
            return kind, ref
    return "unknown", None


def _extract_structured_issues(
    evaluation: dict,
    cases: list[SandboxTestCase],
) -> list[dict]:
    """从 evaluation 中聚合结构化问题清单 (SandboxIssue[])。"""
    issues = []
    issue_idx = 0

    # 从质量扣分项
    for d in evaluation.get("quality_detail", {}).get("top_deductions", []):
        reason = d.get("reason", "")
        target_kind, target_ref = _infer_target_kind(reason)

        # 从 case_scores 中找到关联的 case
        source_cases = []
        for i, cs in enumerate(evaluation.get("quality_detail", {}).get("case_scores", [])):
            if cs.get("reason") and reason[:20] in cs.get("reason", ""):
                source_cases.append(i)

        # 从关联 case 中提取 evidence
        evidence_snippets = []
        for ci in source_cases[:3]:
            if ci < len(cases):
                c = cases[ci]
                if c.llm_response:
                    evidence_snippets.append(c.llm_response[:200])
                if c.verdict_reason:
                    evidence_snippets.append(c.verdict_reason[:200])

        # retest_scope: 从关联 case 推导权限组合
        retest_scope = []
        for ci in source_cases:
            if ci < len(cases):
                c = cases[ci]
                combo_key = f"{c.row_visibility}|{c.field_output_semantic}|{c.group_semantic}"
                if combo_key not in retest_scope:
                    retest_scope.append(combo_key)

        severity = "critical" if abs(d.get("points", 0)) >= 15 else "major" if abs(d.get("points", 0)) >= 8 else "minor"

        issues.append({
            "issue_id": f"issue_{uuid.uuid4().hex[:8]}",
            "severity": severity,
            "dimension": d.get("dimension", ""),
            "reason": reason,
            "impact": f"扣分 {d.get('points', 0)} 分",
            "source_cases": source_cases,
            "evidence_snippets": evidence_snippets[:5],
            "fix_suggestion": d.get("fix_suggestion", ""),
            "target_kind": target_kind,
            "target_ref": target_ref or "",
            "retest_scope": retest_scope,
        })
        issue_idx += 1

    # 从易用性
    usability = evaluation.get("usability_detail", {})
    if usability.get("reason"):
        target_kind, target_ref = _infer_target_kind(usability["reason"])
        issues.append({
            "issue_id": f"issue_{uuid.uuid4().hex[:8]}",
            "severity": "major",
            "dimension": "usability",
            "reason": usability["reason"],
            "impact": "易用性未达标",
            "source_cases": [],
            "evidence_snippets": [],
            "fix_suggestion": usability.get("fix_suggestion", ""),
            "target_kind": target_kind,
            "target_ref": target_ref or "",
            "retest_scope": [],
        })

    # 从反幻觉行为验证
    for bc in evaluation.get("anti_hallucination_detail", {}).get("behavior_checks", []):
        if not bc.get("passed"):
            reason = f"缺证据场景编造: {bc.get('prompt', '')[:80]}"
            evidence = []
            if bc.get("response_preview"):
                evidence.append(bc["response_preview"][:200])
            issues.append({
                "issue_id": f"issue_{uuid.uuid4().hex[:8]}",
                "severity": "critical",
                "dimension": "anti_hallucination",
                "reason": reason,
                "impact": "模型在缺证据时编造回答",
                "source_cases": [],
                "evidence_snippets": evidence,
                "fix_suggestion": "在 prompt 中强化拒答指令",
                "target_kind": "skill_prompt",
                "target_ref": "SKILL.md",
                "retest_scope": [],
            })

    return issues


def _extract_structured_fix_plan(
    issues: list[dict],
    evaluation: dict,
) -> list[dict]:
    """从结构化问题清单生成结构化整改计划 (SandboxFixPlanItem[])。"""
    fix_items = []

    # 按 severity 排序: critical > major > minor
    severity_order = {"critical": 0, "major": 1, "minor": 2}
    sorted_issues = sorted(issues, key=lambda x: severity_order.get(x.get("severity", "minor"), 2))

    for idx, issue in enumerate(sorted_issues):
        priority = "p0" if issue["severity"] == "critical" else "p1" if issue["severity"] == "major" else "p2"

        # 根据 target_kind 生成 acceptance_rule
        acceptance_rules = {
            "skill_prompt": f"prompt 中需包含对应限制或指令",
            "knowledge_reference": "知识引用配置正确，RAG 能命中预期条目",
            "tool_binding": "工具绑定正确，调用链路无报错",
            "input_slot_definition": "输入槽位定义完整，来源覆盖所有必填字段",
            "permission_config": "权限配置与业务预期一致",
            "unknown": "对应评分维度分数 ≥ 70",
        }

        # 推断 action_type
        action_types = {
            "skill_prompt": "fix_prompt_logic",
            "knowledge_reference": "fix_knowledge_binding",
            "tool_binding": "fix_tool_usage",
            "input_slot_definition": "fix_input_slot",
            "permission_config": "fix_permission_handling",
            "unknown": "fix_prompt_logic",
        }

        fix_items.append({
            "id": f"fix_{uuid.uuid4().hex[:8]}",
            "title": f"修复: [{issue['dimension']}] {issue['reason'][:80]}",
            "priority": priority,
            "problem_ids": [issue["issue_id"]],
            "action_type": action_types.get(issue["target_kind"], "fix_prompt_logic"),
            "target_kind": issue["target_kind"],
            "target_ref": issue["target_ref"],
            "suggested_changes": issue.get("fix_suggestion", ""),
            "acceptance_rule": acceptance_rules.get(issue["target_kind"], ""),
            "retest_scope": issue.get("retest_scope", []),
            "estimated_gain": issue.get("impact", ""),
        })

    return fix_items


def _extract_supporting_findings(
    evaluation: dict,
    cases: list[SandboxTestCase],
) -> list[dict]:
    """从 evaluation 中提取 supporting 结论（辅助性洞察），包括通过项的亮点和各维度的补充结论。"""
    findings: list[dict] = []

    quality_detail = evaluation.get("quality_detail", {})

    # 1. 从 case_scores 中提取 supporting 结论：分数 >= 70 但有改进空间的用例
    for i, cs in enumerate(quality_detail.get("case_scores", [])):
        score = cs.get("score", 0)
        reason = cs.get("reason", "")
        if not reason:
            continue

        # 只提取有实质性结论的用例（有 reason 且 score 有值）
        deductions = cs.get("deductions", [])
        if score >= 70 and deductions:
            # 通过但有扣分 = supporting 结论（有改进空间）
            evidence = []
            if i < len(cases):
                c = cases[i]
                if c.llm_response:
                    evidence.append(c.llm_response[:300])

            findings.append({
                "id": f"sf_{uuid.uuid4().hex[:8]}",
                "title": f"用例 #{i} 综合 {score} 分 — 有改进空间",
                "conclusion": reason,
                "detail": "; ".join(
                    f"[{d.get('dimension', '')}] -{d.get('points', 0)}分: {d.get('reason', '')}"
                    for d in deductions
                ),
                "evidence_snippets": evidence,
                "source_case_indexes": [i],
                "severity": "info" if score >= 85 else "minor",
                "recommendation": deductions[0].get("fix_suggestion", "") if deductions else "",
            })

    # 2. 易用性维度 supporting：即使通过也提供维度详情
    usability = evaluation.get("usability_detail", {})
    if usability.get("input_burden_score") is not None:
        scores_desc = (
            f"输入负担 {usability.get('input_burden_score', 'N/A')}, "
            f"首轮成功 {usability.get('first_turn_success_score', 'N/A')}, "
            f"精简度 {usability.get('compact_answer_score', 'N/A')}, "
            f"安全精简 {usability.get('safe_compact_answer_score', 'N/A')}"
        )
        usability_passed = evaluation.get("usability_passed", False)
        finding = {
            "id": f"sf_{uuid.uuid4().hex[:8]}",
            "title": "易用性维度综合",
            "conclusion": scores_desc,
            "severity": "info" if usability_passed else "major",
            "source_case_indexes": [],
            "evidence_snippets": [],
        }
        if usability.get("reason"):
            finding["detail"] = usability["reason"]
        if usability.get("fix_suggestion"):
            finding["recommendation"] = usability["fix_suggestion"]
        findings.append(finding)

    # 3. 反幻觉维度 supporting：通过的行为验证也作为 supporting 证据
    anti_hal = evaluation.get("anti_hallucination_detail", {})
    passed_behaviors = [bc for bc in anti_hal.get("behavior_checks", []) if bc.get("passed")]
    if passed_behaviors:
        findings.append({
            "id": f"sf_{uuid.uuid4().hex[:8]}",
            "title": f"反幻觉行为验证 — {len(passed_behaviors)} 项通过",
            "conclusion": "; ".join(
                f"场景: {bc.get('prompt', '')[:60]}... → 正确拒答"
                for bc in passed_behaviors[:3]
            ),
            "severity": "info",
            "source_case_indexes": [],
            "evidence_snippets": [
                bc.get("response_preview", "")[:200]
                for bc in passed_behaviors[:3]
                if bc.get("response_preview")
            ],
        })

    return findings


def _extract_top_issues(evaluation: dict) -> list[dict]:
    """从 evaluation 中聚合 top issues。"""
    issues = []
    # 从质量扣分项
    for d in evaluation.get("quality_detail", {}).get("top_deductions", []):
        issues.append({
            "source": "quality",
            "dimension": d.get("dimension", ""),
            "points": d.get("points", 0),
            "reason": d.get("reason", ""),
        })
    # 从易用性
    usability = evaluation.get("usability_detail", {})
    if usability.get("reason"):
        issues.append({
            "source": "usability",
            "dimension": "usability",
            "reason": usability["reason"],
        })
    # 从反幻觉行为验证
    for bc in evaluation.get("anti_hallucination_detail", {}).get("behavior_checks", []):
        if not bc.get("passed"):
            issues.append({
                "source": "anti_hallucination",
                "dimension": "behavior",
                "reason": f"缺证据场景编造: {bc.get('prompt', '')[:50]}",
            })
    return issues[:5]


def _extract_fix_plan(evaluation: dict) -> list[str]:
    """从 evaluation 中聚合修复建议。"""
    fixes = []
    for d in evaluation.get("quality_detail", {}).get("top_deductions", []):
        if d.get("fix_suggestion"):
            fixes.append(d["fix_suggestion"])
    for f in evaluation.get("quality_detail", {}).get("fix_plan", []):
        if f and f not in fixes:
            fixes.append(f)
    usability_fix = evaluation.get("usability_detail", {}).get("fix_suggestion")
    if usability_fix:
        fixes.append(usability_fix)
    ah_suggestion = evaluation.get("anti_hallucination_detail", {}).get("suggestion")
    if ah_suggestion:
        fixes.append(ah_suggestion)
    return fixes[:5]


async def generate_report(
    session: SandboxTestSession,
    cases: list[SandboxTestCase],
    evaluation: dict,
    db: Session,
) -> SandboxTestReport:
    """生成不可变测试报告并持久化到知识库。"""

    # ── Part 1: 证据检测结果（含证据化审批字段） ──
    part1 = {
        "q1_input_slots": {
            "total_slots": len(session.detected_slots or []),
            "verified": sum(1 for s in (session.detected_slots or []) if s.get("evidence_status") == "verified"),
            "failed": sum(1 for s in (session.detected_slots or []) if s.get("evidence_status") == "failed"),
            "slots": [
                {
                    **slot,
                    "pass_criteria": slot.get("pass_criteria", ""),
                    "decision": slot.get("verification_conclusion", slot.get("evidence_status")),
                    "reason": slot.get("verification_reason", ""),
                    "evidence_ref": slot.get("evidence_ref", ""),
                    "remediation": slot.get("suggested_source", ""),
                }
                for slot in (session.detected_slots or [])
            ],
        },
        "q2_tool_review": {
            "total_tools": len(session.tool_review or []),
            "confirmed": sum(1 for t in (session.tool_review or []) if t.get("confirmed")),
            "must_call": sum(1 for t in (session.tool_review or []) if t.get("decision") == "must_call"),
            "no_need": sum(1 for t in (session.tool_review or []) if t.get("decision") == "no_need"),
            "tools": [
                {
                    "tool_id": t.get("tool_id"),
                    "tool_name": t.get("tool_name"),
                    "decision": t.get("decision", "must_call" if t.get("confirmed") else "unknown"),
                    "no_tool_proof": t.get("no_tool_proof"),
                    "pass_criteria": t.get("pass_criteria", ""),
                    "reason": t.get("requiredness_reason", ""),
                    "evidence_ref": f"tool:{t.get('tool_id')}",
                    "remediation": "",
                    "confirmed": t.get("confirmed"),
                    "input_provenance": t.get("input_provenance", []),
                }
                for t in (session.tool_review or [])
            ],
        },
        "q3_permission_review": {
            "total_tables": len(session.permission_snapshot or []),
            "confirmed": sum(1 for s in (session.permission_snapshot or []) if s.get("confirmed")),
            "included_in_test": sum(1 for s in (session.permission_snapshot or []) if s.get("included_in_test")),
            "no_permission_needed": sum(1 for s in (session.permission_snapshot or []) if s.get("decision") == "no_permission_needed"),
            "tables": [
                {
                    **snap,
                    "pass_criteria": "权限配置符合业务预期" if snap.get("permission_required") else "无需权限控制",
                    "decision": snap.get("decision", "required_confirmed" if snap.get("confirmed") else "unknown"),
                    "reason": snap.get("permission_required_reason", ""),
                    "evidence_ref": f"table:{snap.get('table_name')}",
                    "remediation": "",
                }
                for snap in (session.permission_snapshot or [])
            ],
        },
    }

    # ── Part 2: 测试用例矩阵（含评分细项） ──
    case_results = []
    for c in cases:
        # 尝试从 verdict_reason 解析 scoring_breakdown
        scoring_breakdown = {}
        if c.verdict_reason:
            try:
                parsed = json.loads(c.verdict_reason)
                if isinstance(parsed, dict):
                    scoring_breakdown = parsed
            except (json.JSONDecodeError, TypeError):
                scoring_breakdown = {"reason": c.verdict_reason}

        case_results.append({
            "case_index": c.case_index,
            "row_visibility": c.row_visibility,
            "field_output_semantic": c.field_output_semantic,
            "group_semantic": c.group_semantic,
            "tool_precondition": c.tool_precondition,
            "input_provenance": c.input_provenance,
            "test_input": c.test_input or "",
            "llm_response": c.llm_response or "",
            "test_input_preview": (c.test_input or "")[:200],
            "llm_response_preview": (c.llm_response or "")[:300],
            "verdict": c.verdict.value if c.verdict else None,
            "verdict_reason": c.verdict_reason,
            "execution_duration_ms": c.execution_duration_ms,
            # 新增字段
            "permissions_applied": [
                snap["table_name"]
                for snap in (session.permission_snapshot or [])
                if snap.get("included_in_test")
            ],
            "tool_decision": {
                str(t.get("tool_id")): t.get("decision", "must_call" if t.get("confirmed") else "unknown")
                for t in (session.tool_review or [])
            },
            "slot_coverage": {
                s["slot_key"]: s.get("verification_conclusion", s.get("evidence_status"))
                for s in (session.detected_slots or [])
            },
            "scoring_breakdown": scoring_breakdown,
        })

    part2 = {
        "theoretical_combo_count": session.theoretical_combo_count,
        "semantic_combo_count": session.semantic_combo_count,
        "executed_case_count": session.executed_case_count,
        "collapsed_by_semantics": (session.theoretical_combo_count or 0) - (session.semantic_combo_count or 0),
        "cases": case_results,
        "summary": {
            "passed": sum(1 for c in cases if c.verdict == CaseVerdict.PASSED),
            "failed": sum(1 for c in cases if c.verdict == CaseVerdict.FAILED),
            "error": sum(1 for c in cases if c.verdict == CaseVerdict.ERROR),
            "skipped": sum(1 for c in cases if c.verdict == CaseVerdict.SKIPPED),
        },
    }

    # ── Part 3: 评价（含 Top Issues + Fix Plan + 结构化问题清单） ──
    top_issues = _extract_top_issues(evaluation)
    fix_plan = _extract_fix_plan(evaluation)

    # 结构化问题清单 & 整改计划 & supporting 结论
    structured_issues = _extract_structured_issues(evaluation, cases)
    structured_fix_plan = _extract_structured_fix_plan(structured_issues, evaluation)
    supporting_findings = _extract_supporting_findings(evaluation, cases)

    # 重测建议: 从 issues 的 retest_scope 聚合
    retest_recommendations = []
    for fp_item in structured_fix_plan:
        related_cases = []
        for pid in fp_item.get("problem_ids", []):
            for issue in structured_issues:
                if issue["issue_id"] == pid:
                    related_cases.extend(issue.get("source_cases", []))
        if related_cases:
            retest_recommendations.append({
                "issue_ids": fp_item["problem_ids"],
                "cases": sorted(set(related_cases)),
                "reason": fp_item["title"],
            })

    part3 = {
        "quality": {
            "passed": evaluation.get("quality_passed", False),
            "detail": evaluation.get("quality_detail", {}),
            "standard": "能以全面、丰富维度和严谨 SOP 解决问题",
        },
        "usability": {
            "passed": evaluation.get("usability_passed", False),
            "detail": evaluation.get("usability_detail", {}),
        },
        "anti_hallucination": {
            "passed": evaluation.get("anti_hallucination_passed", False),
            "detail": evaluation.get("anti_hallucination_detail", {}),
        },
        # 向后兼容
        "top_issues": top_issues,
        "fix_plan": fix_plan,
        # 新增结构化字段
        "issues": structured_issues,
        "fix_plan_structured": structured_fix_plan,
        "supporting_findings": supporting_findings,
        "retest_recommendations": retest_recommendations,
        "final_verdict": {
            "quality_passed": evaluation.get("quality_passed", False),
            "usability_passed": evaluation.get("usability_passed", False),
            "anti_hallucination_passed": evaluation.get("anti_hallucination_passed", False),
            "approval_eligible": all([
                evaluation.get("quality_passed", False),
                evaluation.get("usability_passed", False),
                evaluation.get("anti_hallucination_passed", False),
            ]),
        },
    }

    # ── 计算 hash ──
    report_content = json.dumps({"part1": part1, "part2": part2, "part3": part3}, ensure_ascii=False, sort_keys=True)
    report_hash = hashlib.sha256(report_content.encode("utf-8")).hexdigest()[:32]

    # ── 创建 Report ──
    report = SandboxTestReport(
        session_id=session.id,
        target_type=session.target_type,
        target_id=session.target_id,
        target_version=session.target_version,
        target_name=session.target_name,
        tester_id=session.tester_id,
        part1_evidence_check=part1,
        part2_test_matrix=part2,
        part3_evaluation=part3,
        theoretical_combo_count=session.theoretical_combo_count,
        semantic_combo_count=session.semantic_combo_count,
        executed_case_count=session.executed_case_count,
        quality_passed=evaluation.get("quality_passed"),
        usability_passed=evaluation.get("usability_passed"),
        anti_hallucination_passed=evaluation.get("anti_hallucination_passed"),
        approval_eligible=all([
            evaluation.get("quality_passed", False),
            evaluation.get("usability_passed", False),
            evaluation.get("anti_hallucination_passed", False),
        ]),
        report_hash=report_hash,
    )
    db.add(report)
    db.flush()  # 获取 report.id

    # ── 持久化到知识库（不可变 snapshot，不覆盖旧报告）──
    now = datetime.datetime.utcnow()
    tester_name = ""
    try:
        from app.models.user import User
        tester = db.get(User, session.tester_id)
        tester_name = tester.display_name or tester.username if tester else f"user_{session.tester_id}"
    except Exception:
        tester_name = f"user_{session.tester_id}"

    title = (
        f"{now.strftime('%Y-%m-%d %H:%M')}-{tester_name}-"
        f"{session.target_name}-v{session.target_version or '?'}-沙盒测试报告"
    )

    # 生成人类可读的报告内容
    report_text = _render_report_text(title, part1, part2, part3, session)

    ke = KnowledgeEntry(
        title=title,
        content=report_text,
        category="sandbox_test_report",
        status=KnowledgeStatus.APPROVED,
        created_by=session.tester_id,
        source_type="sandbox_test",
        source_file=f"sandbox_report_{report.id}.md",
    )
    db.add(ke)
    db.flush()

    report.knowledge_entry_id = ke.id
    db.commit()

    logger.info(f"Sandbox test report #{report.id} created for session #{session.id}, knowledge_entry #{ke.id}")
    return report


def _render_report_text(
    title: str,
    part1: dict,
    part2: dict,
    part3: dict,
    session: SandboxTestSession,
) -> str:
    """生成人类可读的 Markdown 格式报告，与前端 Step5Report 展示内容对齐。"""
    lines = [
        f"# {title}",
        "",
        f"- 目标类型: {session.target_type}",
        f"- 目标 ID: {session.target_id}",
        f"- 目标版本: v{session.target_version or '?'}",
        f"- 测试人 ID: {session.tester_id}",
        f"- 测试时间: {session.created_at.strftime('%Y-%m-%d %H:%M') if session.created_at else ''}",
        "",
        "---",
        "",
        "## Part 1: 检测结果",
        "",
        "### Q1 输入槽位来源确认",
        f"- 总槽位数: {part1['q1_input_slots']['total_slots']}",
        f"- 已验证: {part1['q1_input_slots']['verified']}",
        f"- 失败: {part1['q1_input_slots']['failed']}",
        "",
    ]

    for slot in part1["q1_input_slots"]["slots"]:
        decision = slot.get("decision", slot.get("evidence_status", "pending"))
        status_icon = "OK" if decision == "verified" else "FAIL"
        lines.append(
            f"  - [{status_icon}] **{slot.get('label', slot.get('slot_key', ''))}** — "
            f"来源: {slot.get('chosen_source', '未确认')}"
        )
        if slot.get("required_reason"):
            lines.append(f"    - 必填原因: {slot['required_reason']}")
        if slot.get("pass_criteria"):
            lines.append(f"    - 通过标准: {slot['pass_criteria']}")
        if slot.get("reason"):
            lines.append(f"    - 判定理由: {slot['reason']}")
        if slot.get("remediation"):
            lines.append(f"    - 整改建议: {slot['remediation']}")

    lines += [
        "",
        "### Q2 Tool 检测与确认",
        f"- 总工具数: {part1['q2_tool_review']['total_tools']}",
        f"- 必须调用: {part1['q2_tool_review'].get('must_call', 0)}",
        f"- 无需调用: {part1['q2_tool_review'].get('no_need', 0)}",
        "",
    ]

    for t in part1["q2_tool_review"]["tools"]:
        decision = t.get("decision", "unknown")
        icon = "CALL" if decision == "must_call" else "SKIP" if decision == "no_need" else "?"
        lines.append(f"  - [{icon}] **{t.get('tool_name', '')}** (ID: {t.get('tool_id', '')})")
        if t.get("reason"):
            lines.append(f"    - 判定理由: {t['reason']}")
        if t.get("no_tool_proof"):
            lines.append(f"    - 无需调用证明: {t['no_tool_proof']}")
        for prov in t.get("input_provenance", []):
            lines.append(f"    - {prov.get('field_name', '')}: {prov.get('source_kind', 'N/A')} -> {prov.get('source_ref', 'N/A')}")

    lines += [
        "",
        "### Q3 权限快照确认",
        f"- 总数据表: {part1['q3_permission_review']['total_tables']}",
        f"- 已确认: {part1['q3_permission_review']['confirmed']}",
        f"- 纳入测试: {part1['q3_permission_review']['included_in_test']}",
        f"- 无需权限: {part1['q3_permission_review'].get('no_permission_needed', 0)}",
        "",
    ]

    for tbl in part1["q3_permission_review"]["tables"]:
        decision = tbl.get("decision", "unknown")
        icon = "OK" if decision in ("required_confirmed", "no_permission_needed") else "FAIL"
        lines.append(
            f"  - [{icon}] **{tbl.get('display_name', tbl.get('table_name', ''))}** — "
            f"决策: {decision}"
        )
        if tbl.get("reason"):
            lines.append(f"    - 权限理由: {tbl['reason']}")
        if tbl.get("why_no_permission_needed"):
            lines.append(f"    - 无需权限原因: {tbl['why_no_permission_needed']}")
        for rule in tbl.get("applied_rules", []):
            lines.append(f"    - 规则: {rule}")

    lines += [
        "",
        "---",
        "",
        "## Part 2: 权限穷尽测试用例",
        "",
        f"- 理论组合数: {part2.get('theoretical_combo_count', 0)}",
        f"- 语义组合数: {part2.get('semantic_combo_count', 0)}",
        f"- 实际执行数: {part2.get('executed_case_count', 0)}",
        f"- 语义折叠: {part2.get('collapsed_by_semantics', 0)}",
        "",
    ]

    summary = part2.get("summary", {})
    lines += [
        f"**汇总**: 通过 {summary.get('passed', 0)} / 失败 {summary.get('failed', 0)} / "
        f"错误 {summary.get('error', 0)} / 跳过 {summary.get('skipped', 0)}",
        "",
    ]

    # 逐用例展示完整评分论据
    for c in part2.get("cases", []):
        sb = c.get("scoring_breakdown", {})
        score = sb.get("score", "N/A")
        verdict = c.get("verdict", "N/A")
        verdict_icon = "OK" if verdict == "passed" else "FAIL" if verdict == "failed" else "SKIP" if verdict == "skipped" else "ERR"

        lines += [
            f"### 用例 #{c.get('case_index', '?')} [{verdict_icon}] 综合分: {score}",
            "",
            f"| 维度 | 值 |",
            f"|------|----|",
            f"| 行可见范围 | {c.get('row_visibility', '')} |",
            f"| 字段输出语义 | {c.get('field_output_semantic', '')} |",
            f"| 分组语义 | {c.get('group_semantic', '')} |",
            f"| Tool 前置条件 | {c.get('tool_precondition', '')} |",
            "",
        ]

        # 四维评分
        if any(sb.get(k) is not None for k in ("coverage_score", "correctness_score", "constraint_score", "actionability_score")):
            lines += [
                f"**四维评分:**",
                f"- 覆盖度 (coverage): {sb.get('coverage_score', 'N/A')} — 是否解决核心问题",
                f"- 正确性 (correctness): {sb.get('correctness_score', 'N/A')} — 回答是否准确、无幻觉",
                f"- 约束遵守 (constraint): {sb.get('constraint_score', 'N/A')} — 是否遵守权限限制",
                f"- 可行动性 (actionability): {sb.get('actionability_score', 'N/A')} — 输出是否可直接用于决策",
                "",
            ]

        # 主问题 + 扣分论据
        main_issue = sb.get("main_issue") or sb.get("reason") or ""
        if main_issue:
            lines.append(f"**主问题:** {main_issue}")
            lines.append("")

        deductions = sb.get("deductions", [])
        if deductions:
            lines.append("**扣分论据:**")
            for d in deductions:
                lines.append(
                    f"- [{d.get('dimension', '')}] {d.get('points', 0)}分: "
                    f"{d.get('reason', '')}"
                )
                if d.get("fix_suggestion"):
                    lines.append(f"  - 修复建议: {d['fix_suggestion']}")
            lines.append("")

        fix = sb.get("fix_suggestion", "")
        if fix:
            lines.append(f"**整改建议:** {fix}")
            lines.append("")

        # LLM 输出片段（前端页面能看到，报告也要有）
        llm_preview = c.get("llm_response", "") or c.get("llm_response_preview", "")
        if llm_preview:
            lines += [
                "**AI 输出片段:**",
                "```",
                llm_preview,
                "```",
                "",
            ]

        # 测试输入片段
        input_preview = c.get("test_input", "") or c.get("test_input_preview", "")
        if input_preview:
            lines += [
                "**测试输入片段:**",
                "```",
                input_preview,
                "```",
                "",
            ]

        lines.append("---")
        lines.append("")

    lines += [
        "## Part 3: 评价",
        "",
    ]

    # 3.1 质量
    q = part3.get("quality", {})
    qd = q.get("detail", {})
    lines += [
        f"### 3.1 质量 — {'OK 通过' if q.get('passed') else 'FAIL 未通过'}",
        "",
        f"- **评判标准**: 四维度各 0-100 分，加权综合 >={QUALITY_PASS_THRESHOLD} 为通过",
        *[
            f"  - {item['label']} ({item['weight']}%): {item['description']}"
            for item in QUALITY_DIMENSIONS
        ],
        "",
        f"- **综合分: {qd.get('avg_score', 'N/A')}** (阈值 {QUALITY_PASS_THRESHOLD})",
        f"- 覆盖度: {qd.get('avg_coverage', 'N/A')} | 正确性: {qd.get('avg_correctness', 'N/A')} | "
        f"约束: {qd.get('avg_constraint', 'N/A')} | 可行动: {qd.get('avg_actionability', 'N/A')}",
        "",
    ]

    # 逐用例评分明细
    case_scores = qd.get("case_scores", [])
    if case_scores:
        lines.append("**逐用例评分明细:**")
        lines.append("")
        lines.append("| 用例 | 综合 | 覆盖 | 正确 | 约束 | 可行动 | 主问题 |")
        lines.append("|------|------|------|------|------|--------|--------|")
        for i, cs in enumerate(case_scores):
            reason = (cs.get("reason") or "")[:40]
            lines.append(
                f"| #{i} "
                f"| {cs.get('score', 'N/A')} "
                f"| {cs.get('coverage_score', 'N/A')} "
                f"| {cs.get('correctness_score', 'N/A')} "
                f"| {cs.get('constraint_score', 'N/A')} "
                f"| {cs.get('actionability_score', 'N/A')} "
                f"| {reason} |"
            )
        lines.append("")

    top_deductions = qd.get("top_deductions", [])
    if top_deductions:
        lines.append("**主要扣分项（按扣分绝对值排序）:**")
        for d in top_deductions:
            lines.append(
                f"  - [{d.get('dimension', '')}] {d.get('points', 0)}分: "
                f"{d.get('reason', '')}"
            )
            if d.get("fix_suggestion"):
                lines.append(f"    → 修复建议: {d['fix_suggestion']}")
        lines.append("")

    # 3.2 易用性
    u = part3.get("usability", {})
    ud = u.get("detail", {})
    lines += [
        f"### 3.2 易用性 — {'OK 通过' if u.get('passed') else 'FAIL 未通过'}",
        "",
        f"- **评判标准**: 四维度各 0-100，通过条件：输入负担 ≥60、首轮成功 ≥70、安全精简 ≥70",
        f"  - 输入负担: 用户需手动填的结构化信息越少越好（数据表/知识库自动取数不算负担）",
        f"  - 首轮成功: 用户一句话能否得到可用结果，不需多轮澄清",
        f"  - 精简度: 30 字内能否给结论型回答",
        f"  - 安全精简: 精简到短答案时是否仍不引入幻觉",
        "",
    ]
    if ud.get("input_burden_score") is not None:
        ib = ud.get("input_burden_score", 0)
        ft = ud.get("first_turn_success_score", 0)
        ca = ud.get("compact_answer_score", 0)
        sc = ud.get("safe_compact_answer_score", 0)
        lines += [
            f"| 维度 | 分数 | 阈值 | 判定 |",
            f"|------|------|------|------|",
            f"| 输入负担 | {ib} | 60 | {'OK' if ib >= 60 else 'FAIL'} |",
            f"| 首轮成功 | {ft} | 70 | {'OK' if ft >= 70 else 'FAIL'} |",
            f"| 精简度 | {ca} | — | — |",
            f"| 安全精简 | {sc} | 70 | {'OK' if sc >= 70 else 'FAIL'} |",
            "",
        ]
    if ud.get("reason"):
        lines.append(f"**LLM 评审理由:** {ud['reason']}")
        lines.append("")
    if ud.get("fix_suggestion"):
        lines.append(f"**修复建议:** {ud['fix_suggestion']}")
        lines.append("")

    # 3.3 反幻觉
    a = part3.get("anti_hallucination", {})
    ad = a.get("detail", {})
    lines += [
        f"### 3.3 大模型幻觉限制 — {'OK 通过' if a.get('passed') else 'FAIL 未通过'}",
        "",
        f"- **评判标准**: prompt 中必须包含三类反幻觉关键词 + 缺证据场景模型必须拒答",
        "",
    ]
    # 关键词检查
    lines.append("**关键词静态检查:**")
    lines.append("")
    for chk in ad.get("keyword_checks", ad.get("checks", [])):
        icon = "OK" if chk.get("found") else "FAIL"
        keywords = chk.get("keywords_searched", [])
        kw_str = f"（搜索词：{', '.join(keywords[:5])}）" if keywords else ""
        lines.append(f"- [{icon}] {chk.get('check', '')} {kw_str}")
    lines.append("")

    # 行为验证
    behavior_checks = ad.get("behavior_checks", [])
    if behavior_checks:
        lines.append("**行为验证（缺证据场景拒答测试）:**")
        lines.append("")
        for bc in behavior_checks:
            icon = "OK" if bc.get("passed") else "FAIL"
            lines.append(f"- [{icon}] 测试场景: {bc.get('prompt', '')}")
            lines.append(f"  - 是否拒答: {'是' if bc.get('refused') else '否'}")
            lines.append(f"  - 是否编造: {'是' if bc.get('fabricated') else '否'}")
            if bc.get("response_preview"):
                lines.append(f"  - 模型回复片段: {bc['response_preview'][:200]}")
            if bc.get("error"):
                lines.append(f"  - 执行错误: {bc['error']}")
        lines.append("")

    if ad.get("suggestion"):
        lines.append(f"**建议:** {ad['suggestion']}")
        lines.append("")

    # Top Issues + Fix Plan
    top_issues = part3.get("top_issues", [])
    fix_plan = part3.get("fix_plan", [])
    if top_issues:
        lines += ["### Top Issues", ""]
        for i, issue in enumerate(top_issues, 1):
            points_str = f" ({issue['points']}分)" if issue.get("points") else ""
            lines.append(f"  {i}. [{issue.get('source', '')}:{issue.get('dimension', '')}]{points_str} {issue.get('reason', '')}")
        lines.append("")
    if fix_plan:
        lines += ["### Fix Plan", ""]
        for i, fix in enumerate(fix_plan, 1):
            lines.append(f"  {i}. {fix}")
        lines.append("")

    # 结构化问题清单（完整）
    structured_issues = part3.get("issues", [])
    if structured_issues:
        lines += ["### 结构化问题清单（完整）", ""]
        for issue in structured_issues:
            lines.append(
                f"- [{issue.get('severity', 'minor')}] "
                f"{issue.get('issue_id', '')} | {issue.get('dimension', '')} | {issue.get('target_kind', 'unknown')}"
            )
            lines.append(f"  - 原因: {issue.get('reason', '')}")
            if issue.get("impact"):
                lines.append(f"  - 影响: {issue.get('impact', '')}")
            if issue.get("fix_suggestion"):
                lines.append(f"  - 修复建议: {issue.get('fix_suggestion', '')}")
            if issue.get("target_ref"):
                lines.append(f"  - 目标引用: {issue.get('target_ref', '')}")
            if issue.get("source_cases"):
                lines.append(f"  - 关联用例: {issue.get('source_cases')}")
            if issue.get("retest_scope"):
                lines.append(f"  - 重测范围: {issue.get('retest_scope')}")
            for snippet in (issue.get("evidence_snippets") or [])[:3]:
                lines.append(f"  - 证据: {snippet}")
        lines.append("")

    # 结构化整改计划（完整）
    structured_fix_plan = part3.get("fix_plan_structured", [])
    if structured_fix_plan:
        lines += ["### 结构化整改计划（完整）", ""]
        for item in structured_fix_plan:
            lines.append(
                f"- [{item.get('priority', 'p2')}] {item.get('id', '')} | "
                f"{item.get('action_type', 'fix_prompt_logic')} | {item.get('target_kind', 'unknown')}"
            )
            lines.append(f"  - 标题: {item.get('title', '')}")
            if item.get("problem_ids"):
                lines.append(f"  - 关联问题: {item.get('problem_ids')}")
            if item.get("target_ref"):
                lines.append(f"  - 目标引用: {item.get('target_ref')}")
            if item.get("suggested_changes"):
                lines.append(f"  - 建议变更: {item.get('suggested_changes')}")
            if item.get("acceptance_rule"):
                lines.append(f"  - 验收标准: {item.get('acceptance_rule')}")
            if item.get("retest_scope"):
                lines.append(f"  - 重测范围: {item.get('retest_scope')}")
            if item.get("estimated_gain"):
                lines.append(f"  - 预期收益: {item.get('estimated_gain')}")
        lines.append("")

    # Supporting 结论
    supporting = part3.get("supporting_findings", [])
    if supporting:
        lines += ["### Supporting 结论", ""]
        for sf in supporting:
            sev = sf.get("severity", "info").upper()
            lines.append(f"- **[{sev}] {sf.get('title', '')}**")
            if sf.get("conclusion"):
                lines.append(f"  - 结论: {sf['conclusion']}")
            if sf.get("detail"):
                lines.append(f"  - 详情: {sf['detail']}")
            if sf.get("recommendation"):
                lines.append(f"  - 建议: {sf['recommendation']}")
            if sf.get("evidence_snippets"):
                for snippet in sf["evidence_snippets"][:2]:
                    lines.append(f"  - 证据: {snippet[:150]}")
        lines.append("")

    # 最终判定
    fv = part3.get("final_verdict", {})
    lines += [
        "---",
        "",
        "## 最终判定",
        "",
        f"| 维度 | 结果 |",
        f"|------|------|",
        f"| 质量 (综合 >={QUALITY_PASS_THRESHOLD}) | {'OK' if fv.get('quality_passed') else 'FAIL'} |",
        f"| 易用性 (三项阈值) | {'OK' if fv.get('usability_passed') else 'FAIL'} |",
        f"| 幻觉限制 (关键词+行为) | {'OK' if fv.get('anti_hallucination_passed') else 'FAIL'} |",
        f"| **可提交审批** | **{'OK 是' if fv.get('approval_eligible') else 'FAIL 否'}** |",
    ]

    return "\n".join(lines)


def render_preflight_report_text(
    *,
    skill_name: str,
    skill_version: str | int | None,
    gates: list[dict],
    quality_detail: dict,
    tests: list[dict],
) -> str:
    """生成 preflight 专用的 Markdown 报告，章节结构与 interactive 报告对齐。"""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Preflight 报告 — {skill_name} v{skill_version or '?'}",
        "",
        f"- 检测时间: {now}",
        "",
        "---",
        "",
        "## 门检结果",
        "",
    ]

    for gate in gates:
        gate_name = gate.get("gate", gate.get("name", "unknown"))
        passed = gate.get("passed", False)
        icon = "OK" if passed else "FAIL"
        lines.append(f"### [{icon}] {gate_name}")
        lines.append("")

        for item in gate.get("items", []):
            item_passed = item.get("passed", item.get("ok", False))
            item_icon = "OK" if item_passed else "FAIL"
            label = item.get("label", item.get("key", ""))
            detail = item.get("detail", item.get("reason", ""))
            lines.append(f"- [{item_icon}] {label}")
            if detail:
                lines.append(f"  - {detail}")

        lines.append("")

    # 质量评分
    avg_score = quality_detail.get("avg_score", 0)
    lines += [
        "---",
        "",
        "## 质量评分",
        "",
        f"**综合分: {avg_score}** (阈值 {QUALITY_PASS_THRESHOLD})",
        "",
        f"- 覆盖度: {quality_detail.get('avg_coverage', 'N/A')}",
        f"- 正确性: {quality_detail.get('avg_correctness', 'N/A')}",
        f"- 约束遵守: {quality_detail.get('avg_constraint', 'N/A')}",
        f"- 可行动性: {quality_detail.get('avg_actionability', 'N/A')}",
        "",
    ]

    # 逐用例评分
    case_scores = quality_detail.get("case_scores", [])
    if case_scores:
        lines.append("| 用例 | 综合 | 覆盖 | 正确 | 约束 | 可行动 | 主问题 |")
        lines.append("|------|------|------|------|------|--------|--------|")
        for i, cs in enumerate(case_scores):
            reason = (cs.get("reason") or "")[:40]
            lines.append(
                f"| #{i+1} "
                f"| {cs.get('score', 'N/A')} "
                f"| {cs.get('coverage_score', 'N/A')} "
                f"| {cs.get('correctness_score', 'N/A')} "
                f"| {cs.get('constraint_score', 'N/A')} "
                f"| {cs.get('actionability_score', 'N/A')} "
                f"| {reason} |"
            )
        lines.append("")

    # 主要扣分项
    top_deductions = quality_detail.get("top_deductions", [])
    if top_deductions:
        lines.append("**主要扣分项:**")
        lines.append("")
        for d in top_deductions:
            lines.append(
                f"- [{d.get('dimension', '')}] {d.get('points', 0)}分: {d.get('reason', '')}"
            )
            if d.get("fix_suggestion"):
                lines.append(f"  → 修复建议: {d['fix_suggestion']}")
        lines.append("")

    # 测试用例及回复摘要
    if tests:
        lines += [
            "---",
            "",
            "## 测试用例",
            "",
        ]
        for t in tests:
            idx = t.get("index", "?")
            sc = t.get("score", "N/A")
            lines.append(f"### 用例 #{idx} — 评分: {sc}")
            lines.append("")
            if t.get("test_input"):
                lines += ["**测试输入:**", "```", t["test_input"][:500], "```", ""]
            response = t.get("response", "")
            if response:
                lines += ["**AI 回复:**", "```", response[:500], "```", ""]
            detail = t.get("detail", {})
            if detail.get("deductions"):
                lines.append("**扣分项:**")
                for d in detail["deductions"]:
                    lines.append(
                        f"- [{d.get('dimension', '')}] {d.get('points', 0)}分: {d.get('reason', '')}"
                    )
                lines.append("")

    return "\n".join(lines)
