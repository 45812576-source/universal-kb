"""治理自动化引擎：文档上传 → 自动分类 → 高置信度自动生效 → 低置信度排队等人审。

核心流程：
1. governance_auto_classify: 两阶段分类（关键词规则 + LLM fallback）
2. _should_auto_apply: 判断是否可自动生效
3. auto_apply_governance: 自动写入 aligned 状态 + 反馈事件
4. create_review_suggestion: 创建 pending suggestion（含 top-2 候选 + 证据）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_governance import (
    GovernanceBaselineSnapshot,
    GovernanceFeedbackEvent,
    GovernanceObjective,
    GovernanceObjectType,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)

logger = logging.getLogger(__name__)


def _should_auto_apply(confidence: int, strategy_stat: GovernanceStrategyStat | None, threshold: int = 85) -> bool:
    """判断是否可自动生效。

    条件：
    - confidence >= threshold
    - 策略无负面记录（reject_count == 0 或 reject_rate < 20%）
    """
    if confidence < threshold:
        return False
    if strategy_stat and strategy_stat.total_count > 0:
        reject_rate = (strategy_stat.reject_count or 0) / max(strategy_stat.total_count, 1)
        if reject_rate >= 0.2:
            return False
    return True


def auto_apply_governance(db: Session, subject, result: dict[str, Any], subject_type: str = "knowledge") -> None:
    """高置信度自动生效：写入 aligned 状态 + feedback event + strategy stats。"""
    from app.services.knowledge_governance_service import record_governance_feedback

    subject.governance_status = "aligned"
    subject.governance_confidence = result["confidence"] / 100.0
    subject.governance_note = f"自动生效：{result['reason']}"

    if result.get("objective"):
        subject.governance_objective_id = result["objective"].id
    if result.get("library"):
        subject.resource_library_id = result["library"].id
    if result.get("object_type"):
        subject.object_type_id = result["object_type"].id
    if result.get("kr"):
        subject.governance_kr_id = result["kr"].id
    if result.get("element"):
        subject.governance_element_id = result["element"].id

    # 记录正向反馈
    strategy_key = result.get("payload", {}).get("reinforcement_meta", {}).get("strategy_key", "")
    if strategy_key:
        record_governance_feedback(
            db,
            subject_type=subject_type,
            subject_id=subject.id,
            strategy_key=strategy_key,
            event_type="auto_applied",
            reward=0.8,
            created_by=None,
            to_objective_id=subject.governance_objective_id,
            to_resource_library_id=subject.resource_library_id,
            note="自动生效",
        )

    # 创建一条 auto_applied 的 suggestion 用于审计追踪
    task = GovernanceSuggestionTask(
        subject_type=subject_type,
        subject_id=subject.id,
        task_type=result.get("task_type", "classify"),
        status="applied",
        objective_id=subject.governance_objective_id,
        resource_library_id=subject.resource_library_id,
        object_type_id=subject.object_type_id,
        suggested_payload=result.get("payload", {}),
        reason=result["reason"],
        confidence=result["confidence"],
        auto_applied=True,
    )
    db.add(task)


def create_review_suggestion(db: Session, subject, result: dict[str, Any], subject_type: str = "knowledge") -> GovernanceSuggestionTask:
    """低置信度：创建 pending suggestion，写入 top-2 候选及各自证据。"""
    # 构建 candidates payload
    candidates = _build_candidates(result)

    subject.governance_status = "suggested"
    subject.governance_confidence = result["confidence"] / 100.0
    subject.governance_note = result["reason"]

    if result.get("kr"):
        subject.governance_kr_id = result["kr"].id
    if result.get("element"):
        subject.governance_element_id = result["element"].id

    task = GovernanceSuggestionTask(
        subject_type=subject_type,
        subject_id=subject.id,
        task_type=result.get("task_type", "classify"),
        status="pending",
        objective_id=result["objective"].id if result.get("objective") else None,
        resource_library_id=result["library"].id if result.get("library") else None,
        object_type_id=result["object_type"].id if result.get("object_type") else None,
        suggested_payload=result.get("payload", {}),
        reason=result["reason"],
        confidence=result["confidence"],
        auto_applied=False,
        candidates_payload=candidates,
    )
    db.add(task)
    return task


def _build_candidates(result: dict[str, Any]) -> list[dict]:
    """从分类结果构建 top-2 候选列表（含证据）。"""
    primary = {
        "rank": 1,
        "objective_code": result["objective"].code if result.get("objective") else None,
        "library_code": result["library"].code if result.get("library") else None,
        "object_type_code": result["object_type"].code if result.get("object_type") else None,
        "confidence": result.get("confidence", 0),
        "reason": result.get("reason", ""),
        "evidence": result.get("payload", {}).get("keywords", []),
    }
    candidates = [primary]

    # 如果 LLM 返回了第二候选
    llm_candidates = result.get("payload", {}).get("llm_candidates", [])
    if llm_candidates and len(llm_candidates) > 1:
        second = llm_candidates[1]
        candidates.append({
            "rank": 2,
            "objective_code": second.get("objective_code"),
            "library_code": second.get("library_code"),
            "object_type_code": second.get("object_type_code"),
            "confidence": second.get("confidence", 0),
            "reason": second.get("reason", ""),
            "evidence": second.get("evidence", []),
        })

    return candidates


async def _llm_classify(db: Session, entry: KnowledgeEntry) -> dict[str, Any] | None:
    """LLM fallback 分类：当关键词规则置信度不足时调用。"""
    from app.services.llm_gateway import llm_gateway

    content = (entry.content or "")[:3000]
    title = entry.ai_title or entry.title or ""
    summary = entry.ai_summary or ""

    # 获取所有活跃资源库作为分类选项
    libraries = db.query(GovernanceResourceLibrary).filter(
        GovernanceResourceLibrary.is_active == True
    ).all()
    if not libraries:
        return None

    library_options = []
    for lib in libraries:
        obj = db.query(GovernanceObjective).get(lib.objective_id) if lib.objective_id else None
        obj_type = db.query(GovernanceObjectType).filter(
            GovernanceObjectType.code == lib.object_type
        ).first()
        library_options.append({
            "library_code": lib.code,
            "library_name": lib.name,
            "objective_code": obj.code if obj else "",
            "object_type_code": obj_type.code if obj_type else lib.object_type,
            "description": lib.description or "",
        })

    prompt = f"""你是知识治理分类助手。请根据文档内容，从以下资源库中选择最匹配的 1-2 个：

## 可选资源库
{json.dumps(library_options, ensure_ascii=False, indent=2)}

## 文档信息
标题：{title}
摘要：{summary}
内容片段：{content[:1500]}

## 输出格式（JSON）
```json
{{
  "candidates": [
    {{
      "library_code": "...",
      "objective_code": "...",
      "object_type_code": "...",
      "confidence": 75,
      "reason": "匹配原因",
      "evidence": ["关键证据1", "关键证据2"]
    }}
  ]
}}
```
只输出 JSON，不要多余文字。"""

    try:
        config = llm_gateway.resolve_config(db, "governance.classify")
        response, _usage = await llm_gateway.chat(
            config,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )

        # 解析 JSON
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        parsed = json.loads(text.strip())
        candidates = parsed.get("candidates", [])
        if not candidates:
            return None

        top = candidates[0]
        # 查找对应的 DB 对象
        library = db.query(GovernanceResourceLibrary).filter(
            GovernanceResourceLibrary.code == top["library_code"]
        ).first()
        objective = db.query(GovernanceObjective).filter(
            GovernanceObjective.code == top.get("objective_code")
        ).first() if top.get("objective_code") else None
        object_type = db.query(GovernanceObjectType).filter(
            GovernanceObjectType.code == top.get("object_type_code")
        ).first() if top.get("object_type_code") else None

        if not library:
            return None

        # 若 objective 未找到但 library 有 objective_id，用 library 的
        if not objective and library.objective_id:
            objective = db.get(GovernanceObjective, library.objective_id)

        from app.services.knowledge_governance_service import (
            _bandit_adjusted_confidence,
            _find_matching_kr_and_element,
            _field_gap_payload,
            _object_candidates,
            _business_line_for_entry,
        )

        business_line = _business_line_for_entry(db, entry)
        kr, element = _find_matching_kr_and_element(db, library.code)
        field_gap = _field_gap_payload(db, object_type, content)

        adjusted_confidence, reinforcement_meta = _bandit_adjusted_confidence(
            db,
            base_confidence=top.get("confidence", 60),
            strategy_group="llm_classify",
            subject_type="knowledge",
            objective_code=objective.code if objective else None,
            library_code=library.code,
            department_id=entry.department_id,
            business_line=business_line,
        )

        return {
            "objective": objective,
            "library": library,
            "object_type": object_type,
            "kr": kr,
            "element": element,
            "task_type": "classify",
            "reason": f"LLM 分类：{top.get('reason', '')}",
            "confidence": adjusted_confidence,
            "payload": {
                "business_line": business_line,
                "from_llm": True,
                "llm_candidates": candidates,
                "keywords": top.get("evidence", []),
                "kr_id": kr.id if kr else None,
                "element_id": element.id if element else None,
                "object_candidates": _object_candidates(db, object_type, content, business_line),
                "reinforcement_meta": reinforcement_meta,
                **field_gap,
            },
        }
    except Exception as e:
        logger.warning(f"[GovernanceEngine] LLM classify failed: {e}")
        return None


def governance_auto_classify(db: Session, entry: KnowledgeEntry, threshold: int = 85) -> dict[str, Any] | None:
    """两阶段分类：先跑关键词规则 + bandit 置信度，低于阈值时走 LLM fallback。

    返回分类结果 dict（与 infer_governance_suggestion_for_entry 兼容）或 None。
    """
    from app.services.knowledge_governance_service import infer_governance_suggestion_for_entry

    # 阶段 1：关键词规则
    result = infer_governance_suggestion_for_entry(db, entry)
    if result and result["confidence"] >= threshold:
        return result

    # 阶段 2：LLM fallback（仅当配置启用时）
    from app.config import settings
    llm_enabled = getattr(settings, "GOVERNANCE_LLM_ENABLED", True)
    if llm_enabled:
        try:
            loop = asyncio.new_event_loop()
            llm_result = loop.run_until_complete(_llm_classify(db, entry))
            loop.close()
        except Exception as e:
            logger.warning(f"[GovernanceEngine] LLM fallback error: {e}")
            llm_result = None

        # LLM 结果 vs 关键词结果，取置信度更高的
        if llm_result:
            if not result or llm_result["confidence"] > result["confidence"]:
                return llm_result

    # 返回关键词结果（即使低于阈值）
    return result


def process_governance_classify(db: Session, entry: KnowledgeEntry) -> bool:
    """处理单条 governance_classify job 的主入口。

    返回 True 表示处理成功（不论是自动生效还是创建 pending）。
    """
    if entry.governance_status == "aligned":
        return True

    from app.config import settings
    threshold = getattr(settings, "GOVERNANCE_AUTO_APPLY_THRESHOLD", 85)

    result = governance_auto_classify(db, entry, threshold=threshold)
    if not result:
        logger.info(f"[GovernanceEngine] entry {entry.id}: 无法分类，跳过")
        return False

    confidence = result["confidence"]
    strategy_key = result.get("payload", {}).get("reinforcement_meta", {}).get("strategy_key", "")
    strategy_stat = db.query(GovernanceStrategyStat).filter(
        GovernanceStrategyStat.strategy_key == strategy_key
    ).first() if strategy_key else None

    if _should_auto_apply(confidence, strategy_stat, threshold=threshold):
        auto_apply_governance(db, entry, result)
        logger.info(f"[GovernanceEngine] entry {entry.id}: 自动生效 (confidence={confidence})")
    else:
        create_review_suggestion(db, entry, result)
        logger.info(f"[GovernanceEngine] entry {entry.id}: 创建待审 (confidence={confidence})")

    return True


# ── 数据表治理分类 ─────────────────────────────────────────────────────────────


async def _llm_classify_table(db: Session, table: BusinessTable) -> dict[str, Any] | None:
    """LLM fallback 分类（数据表）：拼接 display_name + table_name + description + 列名。"""
    from app.services.llm_gateway import llm_gateway
    from app.models.business import TableField

    # 拼接数据表文本
    fields = db.query(TableField.field_name).filter(TableField.table_id == table.id).all()
    field_names = ", ".join(f.field_name for f in fields) if fields else ""
    text_parts = f"表名：{table.display_name}\n物理表名：{table.table_name}\n描述：{table.description or ''}\n列名：{field_names}"

    libraries = db.query(GovernanceResourceLibrary).filter(
        GovernanceResourceLibrary.is_active == True
    ).all()
    if not libraries:
        return None

    library_options = []
    for lib in libraries:
        obj = db.query(GovernanceObjective).get(lib.objective_id) if lib.objective_id else None
        obj_type = db.query(GovernanceObjectType).filter(
            GovernanceObjectType.code == lib.object_type
        ).first()
        library_options.append({
            "library_code": lib.code,
            "library_name": lib.name,
            "objective_code": obj.code if obj else "",
            "object_type_code": obj_type.code if obj_type else lib.object_type,
            "description": lib.description or "",
        })

    prompt = f"""你是数据治理分类助手。请根据数据表信息，从以下资源库中选择最匹配的 1-2 个：

## 可选资源库
{json.dumps(library_options, ensure_ascii=False, indent=2)}

## 数据表信息
{text_parts}

## 输出格式（JSON）
```json
{{
  "candidates": [
    {{
      "library_code": "...",
      "objective_code": "...",
      "object_type_code": "...",
      "confidence": 75,
      "reason": "匹配原因",
      "evidence": ["关键证据1", "关键证据2"]
    }}
  ]
}}
```
只输出 JSON，不要多余文字。"""

    try:
        config = llm_gateway.resolve_config(db, "governance.classify")
        response, _usage = await llm_gateway.chat(
            config,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )

        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        parsed = json.loads(text.strip())
        candidates = parsed.get("candidates", [])
        if not candidates:
            return None

        top = candidates[0]
        library = db.query(GovernanceResourceLibrary).filter(
            GovernanceResourceLibrary.code == top["library_code"]
        ).first()
        objective = db.query(GovernanceObjective).filter(
            GovernanceObjective.code == top.get("objective_code")
        ).first() if top.get("objective_code") else None
        object_type = db.query(GovernanceObjectType).filter(
            GovernanceObjectType.code == top.get("object_type_code")
        ).first() if top.get("object_type_code") else None

        if not library:
            return None

        if not objective and library.objective_id:
            objective = db.get(GovernanceObjective, library.objective_id)

        from app.services.knowledge_governance_service import (
            _bandit_adjusted_confidence,
            _find_matching_kr_and_element,
            _field_gap_payload,
            _object_candidates,
        )
        from app.models.user import Department

        department = db.get(Department, table.department_id) if table.department_id else None
        business_line = (department.business_unit or "").strip() if department and department.business_unit else None
        kr, element = _find_matching_kr_and_element(db, library.code)
        field_gap = _field_gap_payload(db, object_type, text_parts)

        adjusted_confidence, reinforcement_meta = _bandit_adjusted_confidence(
            db,
            base_confidence=top.get("confidence", 60),
            strategy_group="llm_classify",
            subject_type="business_table",
            objective_code=objective.code if objective else None,
            library_code=library.code,
            department_id=table.department_id,
            business_line=business_line,
        )

        return {
            "objective": objective,
            "library": library,
            "object_type": object_type,
            "kr": kr,
            "element": element,
            "task_type": "align_library",
            "reason": f"LLM 分类：{top.get('reason', '')}",
            "confidence": adjusted_confidence,
            "payload": {
                "business_line": business_line,
                "from_llm": True,
                "llm_candidates": candidates,
                "keywords": top.get("evidence", []),
                "kr_id": kr.id if kr else None,
                "element_id": element.id if element else None,
                "object_candidates": _object_candidates(db, object_type, text_parts, business_line),
                "reinforcement_meta": reinforcement_meta,
                **field_gap,
            },
        }
    except Exception as e:
        logger.warning(f"[GovernanceEngine] LLM classify table failed: {e}")
        return None


def governance_auto_classify_table(db: Session, table: BusinessTable, threshold: int = 85) -> dict[str, Any] | None:
    """数据表两阶段分类：关键词规则 + LLM fallback。"""
    from app.services.knowledge_governance_service import infer_governance_suggestion_for_table

    result = infer_governance_suggestion_for_table(db, table)
    if result and result["confidence"] >= threshold:
        return result

    from app.config import settings
    llm_enabled = getattr(settings, "GOVERNANCE_LLM_ENABLED", True)
    if llm_enabled:
        try:
            loop = asyncio.new_event_loop()
            llm_result = loop.run_until_complete(_llm_classify_table(db, table))
            loop.close()
        except Exception as e:
            logger.warning(f"[GovernanceEngine] LLM table fallback error: {e}")
            llm_result = None

        if llm_result:
            if not result or llm_result["confidence"] > result["confidence"]:
                return llm_result

    return result


def process_governance_classify_subject(db: Session, subject_type: str, subject) -> bool:
    """泛化治理分类主入口：根据 subject_type 分发到对应分类流程。"""
    if subject.governance_status == "aligned":
        return True

    from app.config import settings
    threshold = getattr(settings, "GOVERNANCE_AUTO_APPLY_THRESHOLD", 85)

    if subject_type == "business_table":
        result = governance_auto_classify_table(db, subject, threshold=threshold)
    else:
        result = governance_auto_classify(db, subject, threshold=threshold)

    if not result:
        logger.info(f"[GovernanceEngine] {subject_type} {subject.id}: 无法分类，跳过")
        return False

    confidence = result["confidence"]
    strategy_key = result.get("payload", {}).get("reinforcement_meta", {}).get("strategy_key", "")
    strategy_stat = db.query(GovernanceStrategyStat).filter(
        GovernanceStrategyStat.strategy_key == strategy_key
    ).first() if strategy_key else None

    if _should_auto_apply(confidence, strategy_stat, threshold=threshold):
        auto_apply_governance(db, subject, result, subject_type=subject_type)
        logger.info(f"[GovernanceEngine] {subject_type} {subject.id}: 自动生效 (confidence={confidence})")
    else:
        create_review_suggestion(db, subject, result, subject_type=subject_type)
        logger.info(f"[GovernanceEngine] {subject_type} {subject.id}: 创建待审 (confidence={confidence})")

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3：基线版本化
# ═══════════════════════════════════════════════════════════════════════════════


def _collect_snapshot_data(db: Session) -> dict:
    """收集当前治理体系的全量骨架快照。"""
    objectives = db.query(GovernanceObjective).filter(GovernanceObjective.is_active == True).all()
    libraries = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.is_active == True).all()
    object_types = db.query(GovernanceObjectType).all()
    strategies = db.query(GovernanceStrategyStat).all()

    return {
        "objectives": [
            {"id": o.id, "code": o.code, "name": o.name, "level": o.level}
            for o in objectives
        ],
        "resource_libraries": [
            {"id": l.id, "code": l.code, "name": l.name, "objective_id": l.objective_id, "object_type": l.object_type}
            for l in libraries
        ],
        "object_types": [
            {"id": t.id, "code": t.code, "name": t.name}
            for t in object_types
        ],
        "strategy_stats": [
            {
                "strategy_key": s.strategy_key,
                "total_count": s.total_count,
                "success_count": s.success_count,
                "reject_count": s.reject_count,
                "is_frozen": s.is_frozen,
            }
            for s in strategies
        ],
    }


def _collect_stats_data(db: Session) -> dict:
    """收集当前治理统计指标。"""
    from sqlalchemy import func

    total_entries = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.content.isnot(None)
    ).scalar() or 0

    aligned = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_status == "aligned"
    ).scalar() or 0

    suggested = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_status == "suggested"
    ).scalar() or 0

    ungoverned = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_status.in_(["ungoverned", None])
    ).scalar() or 0

    # 置信度分布
    high_conf = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_confidence >= 0.85
    ).scalar() or 0

    mid_conf = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_confidence >= 0.5,
        KnowledgeEntry.governance_confidence < 0.85,
    ).scalar() or 0

    low_conf = db.query(func.count(KnowledgeEntry.id)).filter(
        KnowledgeEntry.governance_confidence.isnot(None),
        KnowledgeEntry.governance_confidence < 0.5,
    ).scalar() or 0

    # 数据表统计
    total_tables = db.query(func.count(BusinessTable.id)).filter(
        BusinessTable.is_archived == False,
    ).scalar() or 0

    aligned_tables = db.query(func.count(BusinessTable.id)).filter(
        BusinessTable.governance_status == "aligned",
        BusinessTable.is_archived == False,
    ).scalar() or 0

    ungoverned_tables = db.query(func.count(BusinessTable.id)).filter(
        BusinessTable.governance_status.in_(["ungoverned", None]),
        BusinessTable.is_archived == False,
    ).scalar() or 0

    # 缺口数
    pending_suggestions = db.query(func.count(GovernanceSuggestionTask.id)).filter(
        GovernanceSuggestionTask.status == "pending"
    ).scalar() or 0

    # 综合覆盖率（entries + tables）
    total_all = total_entries + total_tables
    aligned_all = aligned + aligned_tables
    coverage_rate = round(aligned_all / max(total_all, 1) * 100, 1)

    return {
        "total_entries": total_entries,
        "aligned": aligned,
        "suggested": suggested,
        "ungoverned": ungoverned,
        "total_tables": total_tables,
        "aligned_tables": aligned_tables,
        "ungoverned_tables": ungoverned_tables,
        "coverage_rate": coverage_rate,
        "confidence_distribution": {
            "high": high_conf,
            "mid": mid_conf,
            "low": low_conf,
        },
        "pending_suggestions": pending_suggestions,
    }


def _next_version(db: Session, version_type: str) -> str:
    """根据版本类型生成下一个版本号。"""
    latest = (
        db.query(GovernanceBaselineSnapshot)
        .filter(GovernanceBaselineSnapshot.version.isnot(None))
        .order_by(GovernanceBaselineSnapshot.created_at.desc())
        .first()
    )

    if not latest or not latest.version:
        return "v0.1"

    parts = latest.version.lstrip("v").split(".")
    major = int(parts[0]) if parts else 0
    minor = int(parts[1]) if len(parts) > 1 else 0

    if version_type == "steady_state":
        return f"v{major + 1}.0"
    else:
        return f"v{major}.{minor + 1}"


def create_baseline_snapshot(
    db: Session,
    *,
    version_type: str = "init",
    created_by: int | None = None,
    auto_confirm: bool = False,
) -> GovernanceBaselineSnapshot:
    """创建基线快照。

    Args:
        version_type: init | governance_round | steady_state | incremental | gap_fill
        created_by: 创建者用户 ID
        auto_confirm: 是否自动确认（定时任务创建的自动确认）
    """
    import datetime

    version = _next_version(db, version_type)
    snapshot_data = _collect_snapshot_data(db)
    stats_data = _collect_stats_data(db)

    snapshot = GovernanceBaselineSnapshot(
        change_type=version_type,
        version=version,
        version_type=version_type,
        snapshot_data=snapshot_data,
        stats_data=stats_data,
        changed_by=created_by,
        is_active=False,
    )

    if auto_confirm:
        snapshot.is_active = True
        snapshot.confirmed_by = created_by
        snapshot.confirmed_at = datetime.datetime.utcnow()
        # 取消旧的 active
        db.query(GovernanceBaselineSnapshot).filter(
            GovernanceBaselineSnapshot.is_active == True,
            GovernanceBaselineSnapshot.id != snapshot.id,
        ).update({"is_active": False})

    db.add(snapshot)
    db.flush()
    logger.info(f"[GovernanceEngine] baseline snapshot created: {version} ({version_type})")
    return snapshot


def confirm_baseline(db: Session, snapshot_id: int, confirmed_by: int) -> GovernanceBaselineSnapshot:
    """确认基线快照，使其成为当前 active 版本。"""
    import datetime

    snapshot = db.get(GovernanceBaselineSnapshot, snapshot_id)
    if not snapshot:
        raise ValueError(f"Snapshot {snapshot_id} not found")

    # 取消旧的 active
    db.query(GovernanceBaselineSnapshot).filter(
        GovernanceBaselineSnapshot.is_active == True,
    ).update({"is_active": False})

    snapshot.is_active = True
    snapshot.confirmed_by = confirmed_by
    snapshot.confirmed_at = datetime.datetime.utcnow()

    logger.info(f"[GovernanceEngine] baseline confirmed: {snapshot.version}")
    return snapshot


def compute_baseline_diff(db: Session, snapshot_id: int) -> dict:
    """计算指定快照与上一个版本的差异。"""
    current = db.get(GovernanceBaselineSnapshot, snapshot_id)
    if not current:
        return {"error": "snapshot not found"}

    # 找上一个版本
    previous = (
        db.query(GovernanceBaselineSnapshot)
        .filter(
            GovernanceBaselineSnapshot.version.isnot(None),
            GovernanceBaselineSnapshot.id < snapshot_id,
        )
        .order_by(GovernanceBaselineSnapshot.id.desc())
        .first()
    )

    if not previous:
        return {
            "current_version": current.version,
            "previous_version": None,
            "diff": "首个版本，无可比对象",
            "added_libraries": [],
            "removed_libraries": [],
            "stats_diff": None,
        }

    curr_data = current.snapshot_data or {}
    prev_data = previous.snapshot_data or {}
    curr_libs = {l["code"] for l in curr_data.get("resource_libraries", [])}
    prev_libs = {l["code"] for l in prev_data.get("resource_libraries", [])}

    curr_stats = current.stats_data or {}
    prev_stats = previous.stats_data or {}

    return {
        "current_version": current.version,
        "previous_version": previous.version,
        "added_libraries": list(curr_libs - prev_libs),
        "removed_libraries": list(prev_libs - curr_libs),
        "stats_diff": {
            "coverage_rate": {
                "current": curr_stats.get("coverage_rate", 0),
                "previous": prev_stats.get("coverage_rate", 0),
                "delta": round(curr_stats.get("coverage_rate", 0) - prev_stats.get("coverage_rate", 0), 1),
            },
            "aligned": {
                "current": curr_stats.get("aligned", 0),
                "previous": prev_stats.get("aligned", 0),
            },
            "pending_suggestions": {
                "current": curr_stats.get("pending_suggestions", 0),
                "previous": prev_stats.get("pending_suggestions", 0),
            },
        },
    }


def auto_snapshot_on_round(db: Session) -> GovernanceBaselineSnapshot | None:
    """定时任务：如果当日有 ≥10 条 auto-apply，自动创建快照。"""
    import datetime
    from sqlalchemy import func

    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    auto_count = db.query(func.count(GovernanceSuggestionTask.id)).filter(
        GovernanceSuggestionTask.auto_applied == True,
        GovernanceSuggestionTask.created_at >= today_start,
    ).scalar() or 0

    if auto_count < 10:
        return None

    # 检查今天是否已有快照
    existing = db.query(GovernanceBaselineSnapshot).filter(
        GovernanceBaselineSnapshot.created_at >= today_start,
        GovernanceBaselineSnapshot.version_type == "governance_round",
    ).first()
    if existing:
        return None

    snapshot = create_baseline_snapshot(
        db,
        version_type="governance_round",
        auto_confirm=True,
    )
    db.commit()
    logger.info(f"[GovernanceEngine] auto snapshot created: {snapshot.version} (auto_count={auto_count})")
    return snapshot


def detect_baseline_deviation(db: Session) -> GovernanceSuggestionTask | None:
    """定时任务：对比当前状态与 active 基线，偏离超阈值则建告警 suggestion。"""
    active = db.query(GovernanceBaselineSnapshot).filter(
        GovernanceBaselineSnapshot.is_active == True,
    ).first()
    if not active or not active.stats_data:
        return None

    current_stats = _collect_stats_data(db)
    baseline_coverage = active.stats_data.get("coverage_rate", 0)
    current_coverage = current_stats.get("coverage_rate", 0)

    # 覆盖率下降超过 10 个百分点告警
    if baseline_coverage - current_coverage > 10:
        task = GovernanceSuggestionTask(
            subject_type="knowledge",
            subject_id=0,  # 系统级告警
            task_type="baseline_deviation",
            status="pending",
            reason=f"治理覆盖率从基线 {baseline_coverage}% 下降至 {current_coverage}%，偏离超过 10%",
            confidence=0,
            suggested_payload={
                "baseline_version": active.version,
                "baseline_coverage": baseline_coverage,
                "current_coverage": current_coverage,
                "deviation": round(baseline_coverage - current_coverage, 1),
            },
        )
        db.add(task)
        db.commit()
        logger.warning(f"[GovernanceEngine] baseline deviation alert: {baseline_coverage}% → {current_coverage}%")
        return task

    return None
