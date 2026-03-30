"""沙盒测试报告生成 + 知识库持久化。

报告三部分：
  Part 1 — Q1/Q2/Q3 检测结果（含证据化审批字段）
  Part 2 — 权限穷尽测试用例矩阵（含评分细项）
  Part 3 — 质量/易用性/反幻觉三项评价 + Top Issues + Fix Plan

报告不可变，生成后写入知识库，不覆盖旧报告。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging

from sqlalchemy.orm import Session

from app.models.sandbox import (
    SandboxTestSession,
    SandboxTestCase,
    SandboxTestReport,
    CaseVerdict,
)
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus

logger = logging.getLogger(__name__)


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

    # ── Part 3: 评价（含 Top Issues + Fix Plan） ──
    top_issues = _extract_top_issues(evaluation)
    fix_plan = _extract_fix_plan(evaluation)

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
        "top_issues": top_issues,
        "fix_plan": fix_plan,
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
    """生成人类可读的 Markdown 格式报告。"""
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
        f"| # | 行可见 | 字段语义 | 分组 | Tool 前置 | 判定 | 分数 | 主问题 |",
        f"|---|--------|----------|------|-----------|------|------|--------|",
    ]

    for c in part2.get("cases", []):
        sb = c.get("scoring_breakdown", {})
        score = sb.get("score", "")
        main_issue = (sb.get("main_issue") or sb.get("reason") or "")[:40]
        lines.append(
            f"| {c.get('case_index', '')} "
            f"| {c.get('row_visibility', '')} "
            f"| {c.get('field_output_semantic', '')} "
            f"| {c.get('group_semantic', '')} "
            f"| {c.get('tool_precondition', '')} "
            f"| {c.get('verdict', '')} "
            f"| {score} "
            f"| {main_issue} |"
        )

    summary = part2.get("summary", {})
    lines += [
        "",
        f"**汇总**: 通过 {summary.get('passed', 0)} / 失败 {summary.get('failed', 0)} / "
        f"错误 {summary.get('error', 0)} / 跳过 {summary.get('skipped', 0)}",
        "",
        "---",
        "",
        "## Part 3: 评价",
        "",
    ]

    # 3.1 质量
    q = part3.get("quality", {})
    qd = q.get("detail", {})
    lines += [
        f"### 3.1 质量 -- {'OK 通过' if q.get('passed') else 'FAIL 未通过'}",
        f"- 标准: {q.get('standard', '')}",
        f"- 综合分: {qd.get('avg_score', 'N/A')}",
        f"- 覆盖度: {qd.get('avg_coverage', 'N/A')} | 正确性: {qd.get('avg_correctness', 'N/A')} | "
        f"约束: {qd.get('avg_constraint', 'N/A')} | 可行动: {qd.get('avg_actionability', 'N/A')}",
        "",
    ]
    top_deductions = qd.get("top_deductions", [])
    if top_deductions:
        lines.append("**主要扣分项:**")
        for d in top_deductions:
            lines.append(
                f"  - [{d.get('dimension', '')}] {d.get('points', 0)} 分: "
                f"{d.get('reason', '')} -> 建议: {d.get('fix_suggestion', '')}"
            )
        lines.append("")

    # 3.2 易用性
    u = part3.get("usability", {})
    ud = u.get("detail", {})
    lines += [
        f"### 3.2 易用性 -- {'OK 通过' if u.get('passed') else 'FAIL 未通过'}",
    ]
    if ud.get("input_burden_score") is not None:
        lines += [
            f"- 输入负担: {ud.get('input_burden_score', 0)} (阈值 60)",
            f"- 首轮成功: {ud.get('first_turn_success_score', 0)} (阈值 70)",
            f"- 精简度: {ud.get('compact_answer_score', 0)}",
            f"- 安全精简: {ud.get('safe_compact_answer_score', 0)} (阈值 70)",
        ]
    if ud.get("reason"):
        lines.append(f"- 原因: {ud['reason']}")
    if ud.get("fix_suggestion"):
        lines.append(f"- 建议: {ud['fix_suggestion']}")
    lines.append("")

    # 3.3 反幻觉
    a = part3.get("anti_hallucination", {})
    ad = a.get("detail", {})
    lines += [
        f"### 3.3 大模型幻觉限制 -- {'OK 通过' if a.get('passed') else 'FAIL 未通过'}",
    ]
    # 关键词检查
    lines.append("**关键词检查:**")
    for chk in ad.get("keyword_checks", ad.get("checks", [])):
        icon = "OK" if chk.get("found") else "FAIL"
        lines.append(f"  - [{icon}] {chk.get('check', '')}")
    # 行为验证
    behavior_checks = ad.get("behavior_checks", [])
    if behavior_checks:
        lines.append("")
        lines.append("**行为验证:**")
        for bc in behavior_checks:
            icon = "OK" if bc.get("passed") else "FAIL"
            lines.append(f"  - [{icon}] 场景: {bc.get('prompt', '')[:50]}")
            if not bc.get("passed") and bc.get("response_preview"):
                lines.append(f"    - 模型回复: {bc['response_preview'][:100]}")
    if ad.get("suggestion"):
        lines.append(f"- 建议: {ad['suggestion']}")
    lines.append("")

    # Top Issues + Fix Plan
    top_issues = part3.get("top_issues", [])
    fix_plan = part3.get("fix_plan", [])
    if top_issues:
        lines += ["### Top Issues", ""]
        for i, issue in enumerate(top_issues, 1):
            lines.append(f"  {i}. [{issue.get('source', '')}] {issue.get('reason', '')}")
        lines.append("")
    if fix_plan:
        lines += ["### Fix Plan", ""]
        for i, fix in enumerate(fix_plan, 1):
            lines.append(f"  {i}. {fix}")
        lines.append("")

    # 最终判定
    fv = part3.get("final_verdict", {})
    lines += [
        "---",
        "",
        "## 最终判定",
        "",
        f"- 质量: {'OK' if fv.get('quality_passed') else 'FAIL'}",
        f"- 易用性: {'OK' if fv.get('usability_passed') else 'FAIL'}",
        f"- 幻觉限制: {'OK' if fv.get('anti_hallucination_passed') else 'FAIL'}",
        f"- **可提交审批: {'OK 是' if fv.get('approval_eligible') else 'FAIL 否'}**",
    ]

    return "\n".join(lines)
