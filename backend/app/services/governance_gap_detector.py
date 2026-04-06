"""缺口检测引擎：自动检测 AI 弱势领域 → 确定性修复自动执行 → 模糊缺口推人。

核心流程：
1. detect_domain_gaps: 扫 strategy_stats 中高拒绝率策略，标为领域缺口
2. detect_coverage_gaps: 扫资源库字段覆盖率 vs 基线期望
3. auto_fix_deterministic: 可确定修复的自动执行
4. push_gap_to_admin: 模糊缺口创建 suggestion
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_governance import (
    GovernanceBaselineSnapshot,
    GovernanceObjectType,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)

logger = logging.getLogger(__name__)

# 拒绝率超过此阈值 + 样本数 ≥ MIN_SAMPLES → 视为领域缺口
_REJECT_RATE_THRESHOLD = 0.4
_MIN_SAMPLES = 10


def detect_domain_gaps(db: Session) -> list[dict[str, Any]]:
    """扫 strategy_stats 中 reject_rate > 阈值且样本 ≥ 10 的策略，标为领域缺口。

    返回缺口列表 [{ strategy_stat, reject_rate, library_code, ... }]。
    """
    stats = (
        db.query(GovernanceStrategyStat)
        .filter(
            GovernanceStrategyStat.total_count >= _MIN_SAMPLES,
            GovernanceStrategyStat.is_frozen == False,
        )
        .all()
    )

    gaps = []
    for stat in stats:
        if stat.total_count <= 0:
            continue
        reject_rate = (stat.reject_count or 0) / stat.total_count
        if reject_rate >= _REJECT_RATE_THRESHOLD:
            gaps.append({
                "stat_id": stat.id,
                "strategy_key": stat.strategy_key,
                "strategy_group": stat.strategy_group,
                "library_code": stat.library_code,
                "objective_code": stat.objective_code,
                "department_id": stat.department_id,
                "business_line": stat.business_line,
                "reject_rate": round(reject_rate, 4),
                "total_count": stat.total_count,
                "reject_count": stat.reject_count,
                "gap_type": "high_reject_rate",
                "severity": "high" if reject_rate >= 0.6 else "medium",
            })

    if gaps:
        logger.info(f"[GapDetector] detected {len(gaps)} domain gaps")
    return gaps


def detect_coverage_gaps(db: Session) -> list[dict[str, Any]]:
    """扫资源库字段覆盖率 vs 基线期望。

    检测哪些资源库的内容覆盖率显著低于其他库。
    """
    from sqlalchemy import func

    libraries = db.query(GovernanceResourceLibrary).filter(
        GovernanceResourceLibrary.is_active == True,
    ).all()

    gaps = []
    for lib in libraries:
        entry_total = db.query(func.count(KnowledgeEntry.id)).filter(
            KnowledgeEntry.resource_library_id == lib.id,
        ).scalar() or 0

        entry_aligned = db.query(func.count(KnowledgeEntry.id)).filter(
            KnowledgeEntry.resource_library_id == lib.id,
            KnowledgeEntry.governance_status == "aligned",
        ).scalar() or 0

        table_total = db.query(func.count(BusinessTable.id)).filter(
            BusinessTable.resource_library_id == lib.id,
            BusinessTable.is_archived == False,
        ).scalar() or 0

        table_aligned = db.query(func.count(BusinessTable.id)).filter(
            BusinessTable.resource_library_id == lib.id,
            BusinessTable.governance_status == "aligned",
            BusinessTable.is_archived == False,
        ).scalar() or 0

        total = entry_total + table_total
        aligned = entry_aligned + table_aligned

        if total < 3:
            # 资源库内容太少，标为覆盖缺口
            gaps.append({
                "library_id": lib.id,
                "library_code": lib.code,
                "library_name": lib.name,
                "total_entries": total,
                "aligned_entries": aligned,
                "coverage_rate": 0 if total == 0 else round(aligned / total * 100, 1),
                "gap_type": "low_coverage",
                "severity": "high" if total == 0 else "medium",
                "reason": f"资源库 '{lib.name}' 仅有 {total} 条内容",
            })
        elif total > 0 and aligned / total < 0.3:
            gaps.append({
                "library_id": lib.id,
                "library_code": lib.code,
                "library_name": lib.name,
                "total_entries": total,
                "aligned_entries": aligned,
                "coverage_rate": round(aligned / total * 100, 1),
                "gap_type": "low_alignment",
                "severity": "medium",
                "reason": f"资源库 '{lib.name}' 对齐率仅 {round(aligned / total * 100, 1)}%",
            })

    if gaps:
        logger.info(f"[GapDetector] detected {len(gaps)} coverage gaps")
    return gaps


def auto_fix_deterministic(db: Session, gap: dict[str, Any]) -> bool:
    """可确定修复的自动执行。

    目前支持：对已有 resource_library_id 但 governance_status 还是 ungoverned 的条目自动标为 suggested。
    返回 True 表示已执行修复。
    """
    if gap.get("gap_type") != "low_alignment":
        return False

    library_id = gap.get("library_id")
    if not library_id:
        return False

    # 找到属于该库但 ungoverned 的条目，为它们创建 governance_classify job
    from app.models.knowledge_job import KnowledgeJob

    entries = (
        db.query(KnowledgeEntry.id)
        .filter(
            KnowledgeEntry.resource_library_id == library_id,
            KnowledgeEntry.governance_status.in_(["ungoverned", None]),
            KnowledgeEntry.content.isnot(None),
        )
        .limit(20)
        .all()
    )

    # 检查是否已有 queued job（知识条目）
    existing_ids = {
        eid for (eid,) in db.query(KnowledgeJob.knowledge_id).filter(
            KnowledgeJob.job_type == "governance_classify",
            KnowledgeJob.status.in_(["queued", "running"]),
            KnowledgeJob.subject_type == "knowledge",
        ).all()
    }

    created = 0
    for (eid,) in entries:
        if eid not in existing_ids:
            db.add(KnowledgeJob(
                knowledge_id=eid,
                job_type="governance_classify",
                trigger_source="gap_fix",
            ))
            created += 1

    # 同时处理属于该库但 ungoverned 的数据表
    tables = (
        db.query(BusinessTable.id)
        .filter(
            BusinessTable.resource_library_id == library_id,
            BusinessTable.governance_status.in_(["ungoverned", None]),
            BusinessTable.is_archived == False,
        )
        .limit(20)
        .all()
    )

    existing_table_ids = {
        tid for (tid,) in db.query(KnowledgeJob.subject_id).filter(
            KnowledgeJob.subject_type == "business_table",
            KnowledgeJob.job_type == "governance_classify",
            KnowledgeJob.status.in_(["queued", "running"]),
        ).all()
    }

    for (tid,) in tables:
        if tid not in existing_table_ids:
            db.add(KnowledgeJob(
                subject_type="business_table",
                subject_id=tid,
                job_type="governance_classify",
                trigger_source="gap_fix",
            ))
            created += 1

    if created:
        logger.info(f"[GapDetector] auto_fix: created {created} governance_classify jobs for library {library_id}")
    return created > 0


def push_gap_to_admin(db: Session, gap: dict[str, Any]) -> GovernanceSuggestionTask:
    """模糊缺口创建 suggestion (task_type=gap_fix)。"""
    reason_parts = []
    if gap.get("gap_type") == "high_reject_rate":
        reason_parts.append(
            f"策略 '{gap.get('strategy_group', '')}' 在 {gap.get('library_code', '?')} 领域拒绝率 "
            f"{round(gap.get('reject_rate', 0) * 100, 1)}%（样本 {gap.get('total_count', 0)}），"
            "建议补充该领域资料或调整分类策略"
        )
    elif gap.get("gap_type") == "low_coverage":
        reason_parts.append(gap.get("reason", "资源库覆盖不足"))
    else:
        reason_parts.append(gap.get("reason", "领域缺口"))

    task = GovernanceSuggestionTask(
        subject_type="knowledge",
        subject_id=0,  # 系统级
        task_type="gap_fix",
        status="pending",
        reason="; ".join(reason_parts),
        confidence=0,
        suggested_payload={
            "gap": gap,
        },
    )
    db.add(task)
    logger.info(f"[GapDetector] pushed gap to admin: {gap.get('gap_type')} / {gap.get('library_code', '?')}")
    return task


def run_gap_detection(db: Session) -> dict[str, Any]:
    """主入口：运行所有缺口检测 → 确定性修复 → 模糊缺口推人。"""
    domain_gaps = detect_domain_gaps(db)
    coverage_gaps = detect_coverage_gaps(db)

    auto_fixed = 0
    pushed = 0

    for gap in coverage_gaps:
        if auto_fix_deterministic(db, gap):
            auto_fixed += 1
        elif gap.get("severity") in ("high", "medium"):
            push_gap_to_admin(db, gap)
            pushed += 1

    for gap in domain_gaps:
        push_gap_to_admin(db, gap)
        pushed += 1

    if auto_fixed or pushed:
        db.commit()

    return {
        "domain_gaps": len(domain_gaps),
        "coverage_gaps": len(coverage_gaps),
        "auto_fixed": auto_fixed,
        "pushed_to_admin": pushed,
    }
