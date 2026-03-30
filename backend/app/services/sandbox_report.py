"""沙盒测试报告生成 + 知识库持久化。

报告三部分：
  Part 1 — Q1/Q2/Q3 检测结果
  Part 2 — 权限穷尽测试用例矩阵
  Part 3 — 质量/易用性/反幻觉三项评价

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


async def generate_report(
    session: SandboxTestSession,
    cases: list[SandboxTestCase],
    evaluation: dict,
    db: Session,
) -> SandboxTestReport:
    """生成不可变测试报告并持久化到知识库。"""

    # ── Part 1: 证据检测结果 ──
    part1 = {
        "q1_input_slots": {
            "total_slots": len(session.detected_slots or []),
            "verified": sum(1 for s in (session.detected_slots or []) if s.get("evidence_status") == "verified"),
            "failed": sum(1 for s in (session.detected_slots or []) if s.get("evidence_status") == "failed"),
            "slots": session.detected_slots or [],
        },
        "q2_tool_review": {
            "total_tools": len(session.tool_review or []),
            "confirmed": sum(1 for t in (session.tool_review or []) if t.get("confirmed")),
            "tools": [
                {
                    "tool_id": t.get("tool_id"),
                    "tool_name": t.get("tool_name"),
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
            "tables": session.permission_snapshot or [],
        },
    }

    # ── Part 2: 测试用例矩阵 ──
    case_results = []
    for c in cases:
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

    # ── Part 3: 评价 ──
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
        status_icon = "✓" if slot.get("evidence_status") == "verified" else "✗"
        lines.append(
            f"  - {status_icon} **{slot.get('label', slot.get('slot_key', ''))}** — "
            f"来源: {slot.get('chosen_source', '未确认')}, "
            f"状态: {slot.get('evidence_status', 'pending')}"
        )

    lines += [
        "",
        "### Q2 Tool 检测与确认",
        f"- 总工具数: {part1['q2_tool_review']['total_tools']}",
        f"- 已确认: {part1['q2_tool_review']['confirmed']}",
        "",
    ]

    for t in part1["q2_tool_review"]["tools"]:
        icon = "✓" if t.get("confirmed") else "✗"
        lines.append(f"  - {icon} **{t.get('tool_name', '')}** (ID: {t.get('tool_id', '')})")
        for prov in t.get("input_provenance", []):
            lines.append(f"    - {prov.get('field_name', '')}: {prov.get('source_kind', 'N/A')} → {prov.get('source_ref', 'N/A')}")

    lines += [
        "",
        "### Q3 权限快照确认",
        f"- 总数据表: {part1['q3_permission_review']['total_tables']}",
        f"- 已确认: {part1['q3_permission_review']['confirmed']}",
        f"- 纳入测试: {part1['q3_permission_review']['included_in_test']}",
        "",
    ]

    for tbl in part1["q3_permission_review"]["tables"]:
        icon = "✓" if tbl.get("confirmed") else "✗"
        lines.append(
            f"  - {icon} **{tbl.get('display_name', tbl.get('table_name', ''))}** — "
            f"行可见: {tbl.get('row_visibility', 'N/A')}, "
            f"遮罩字段: {len(tbl.get('field_masks', []))} 个"
        )

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
        f"| # | 行可见 | 字段语义 | 分组 | Tool 前置 | 判定 | 原因 |",
        f"|---|--------|----------|------|-----------|------|------|",
    ]

    for c in part2.get("cases", []):
        lines.append(
            f"| {c.get('case_index', '')} "
            f"| {c.get('row_visibility', '')} "
            f"| {c.get('field_output_semantic', '')} "
            f"| {c.get('group_semantic', '')} "
            f"| {c.get('tool_precondition', '')} "
            f"| {c.get('verdict', '')} "
            f"| {(c.get('verdict_reason') or '')[:50]} |"
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

    q = part3.get("quality", {})
    lines += [
        f"### 3.1 质量 — {'✓ 通过' if q.get('passed') else '✗ 未通过'}",
        f"- 标准: {q.get('standard', '')}",
        f"- 平均分: {q.get('detail', {}).get('avg_score', 'N/A')}",
        "",
    ]

    u = part3.get("usability", {})
    lines += [
        f"### 3.2 易用性 — {'✓ 通过' if u.get('passed') else '✗ 未通过'}",
        f"- 结构化手动输入数: {u.get('detail', {}).get('structured_input_count', 0)} (阈值: 5)",
    ]
    if u.get("detail", {}).get("suggestion"):
        lines.append(f"- 建议: {u['detail']['suggestion']}")
    lines.append("")

    a = part3.get("anti_hallucination", {})
    lines += [
        f"### 3.3 大模型幻觉限制 — {'✓ 通过' if a.get('passed') else '✗ 未通过'}",
    ]
    for chk in a.get("detail", {}).get("checks", []):
        icon = "✓" if chk.get("found") else "✗"
        lines.append(f"  - {icon} {chk.get('check', '')}")
    if a.get("detail", {}).get("suggestion"):
        lines.append(f"- 建议: {a['detail']['suggestion']}")
    lines.append("")

    fv = part3.get("final_verdict", {})
    lines += [
        "---",
        "",
        "## 最终判定",
        "",
        f"- 质量: {'✓' if fv.get('quality_passed') else '✗'}",
        f"- 易用性: {'✓' if fv.get('usability_passed') else '✗'}",
        f"- 幻觉限制: {'✓' if fv.get('anti_hallucination_passed') else '✗'}",
        f"- **可提交审批: {'✓ 是' if fv.get('approval_eligible') else '✗ 否'}**",
    ]

    return "\n".join(lines)
