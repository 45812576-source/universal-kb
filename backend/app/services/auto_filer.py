"""自动归档服务：分类树优先，全自动落库，支持撤销。

归档优先级：
1. taxonomy_code → 系统归档树精确节点
2. taxonomy_board → 板块根目录
3. 相似已归档文档的 folder 分布
4. 无法归档 → 跳过

所有自动归档操作写入 knowledge_filing_actions，支持单条/批量撤销。
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_filing import KnowledgeFilingAction

logger = logging.getLogger(__name__)

# 高置信度阈值：直接归档
_HIGH_CONFIDENCE = 0.6
# 低置信度：归到板块根目录
_LOW_CONFIDENCE = 0.3


def _resolve_target_folder(
    db: Session,
    entry: KnowledgeEntry,
) -> Optional[dict]:
    """为单条文档决定归档目标。返回 {folder_id, confidence, reason, decision_source} 或 None。"""
    from app.services.system_folder_service import get_system_folder_for_taxonomy, get_system_folder_for_board

    # 策略1：taxonomy_code 精确匹配
    if entry.taxonomy_code:
        fid = get_system_folder_for_taxonomy(db, entry.taxonomy_code)
        if fid:
            return {
                "folder_id": fid,
                "confidence": min(0.95, (entry.classification_confidence or 0.7)),
                "reason": f"分类 {entry.taxonomy_code} 精确匹配系统目录",
                "decision_source": "taxonomy",
            }

    # 策略2：taxonomy_board 模糊匹配
    if entry.taxonomy_board:
        # 先看同 board 已归档文档的分布
        same_board = (
            db.query(KnowledgeEntry.folder_id)
            .filter(
                KnowledgeEntry.taxonomy_board == entry.taxonomy_board,
                KnowledgeEntry.folder_id.isnot(None),
                KnowledgeEntry.id != entry.id,
            )
            .limit(100)
            .all()
        )
        if same_board:
            counter = Counter(fid for (fid,) in same_board if fid)
            if counter:
                top_fid, top_count = counter.most_common(1)[0]
                conf = min(0.8, top_count / len(same_board))
                if conf >= _LOW_CONFIDENCE:
                    return {
                        "folder_id": top_fid,
                        "confidence": round(conf, 2),
                        "reason": f"同板块 {entry.taxonomy_board} 下 {top_count}/{len(same_board)} 篇文档归于此",
                        "decision_source": "board_neighbors",
                    }

        # fallback: 板块根目录
        board_fid = get_system_folder_for_board(db, entry.taxonomy_board)
        if board_fid:
            return {
                "folder_id": board_fid,
                "confidence": 0.3,
                "reason": f"板块 {entry.taxonomy_board} 根目录（低置信度）",
                "decision_source": "taxonomy",
            }

    return None


def auto_file_single(
    db: Session,
    entry: KnowledgeEntry,
    batch_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[KnowledgeFilingAction]:
    """对单条未归档文档执行自动归档。已归档的跳过。"""
    if entry.folder_id is not None:
        return None

    result = _resolve_target_folder(db, entry)
    if not result:
        return None

    old_folder_id = entry.folder_id
    entry.folder_id = result["folder_id"]

    action = KnowledgeFilingAction(
        knowledge_id=entry.id,
        action_type="auto_file",
        from_folder_id=old_folder_id,
        to_folder_id=result["folder_id"],
        decision_source=result["decision_source"],
        confidence=result["confidence"],
        reason=result["reason"],
        batch_id=batch_id,
        created_by=user_id,
    )
    db.add(action)
    return action


def auto_file_batch(
    db: Session,
    user_id: Optional[int] = None,
    limit: int = 200,
) -> dict:
    """批量自动归档所有未归档文档。

    高置信度（≥ 0.6）直接归档；低置信度生成 suggestion 待审。
    返回统计结果 + suggestions 列表。
    """
    from app.models.knowledge_filing import KnowledgeFilingSuggestion

    batch_id = f"batch-{uuid.uuid4().hex[:12]}"

    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.folder_id.is_(None))
        .limit(limit)
        .all()
    )

    stats = {
        "total": len(entries), "filed": 0, "high_confidence": 0,
        "low_confidence": 0, "skipped": 0, "batch_id": batch_id,
        "suggestions": [],
    }

    for entry in entries:
        result = _resolve_target_folder(db, entry)
        if not result:
            stats["skipped"] += 1
            continue

        if result["confidence"] >= _HIGH_CONFIDENCE:
            # 高置信度：直接归档（inline，避免 _resolve_target_folder 重复调用）
            old_folder_id = entry.folder_id
            entry.folder_id = result["folder_id"]
            action = KnowledgeFilingAction(
                knowledge_id=entry.id,
                action_type="auto_file",
                from_folder_id=old_folder_id,
                to_folder_id=result["folder_id"],
                decision_source=result["decision_source"],
                confidence=result["confidence"],
                reason=result["reason"],
                batch_id=batch_id,
                created_by=user_id,
            )
            db.add(action)
            stats["filed"] += 1
            stats["high_confidence"] += 1
        else:
            # 低置信度：生成 suggestion 待人工审阅
            suggestion = KnowledgeFilingSuggestion(
                knowledge_id=entry.id,
                suggested_folder_id=result["folder_id"],
                suggested_folder_path=result.get("reason", ""),
                confidence=result["confidence"],
                reason=result["reason"],
                based_on={"decision_source": result["decision_source"], "batch_id": batch_id},
                status="pending",
            )
            db.add(suggestion)
            db.flush()
            stats["low_confidence"] += 1
            stats["suggestions"].append({
                "id": suggestion.id,
                "knowledge_id": entry.id,
                "title": entry.ai_title or entry.title,
                "suggested_folder_id": result["folder_id"],
                "confidence": result["confidence"],
                "reason": result["reason"],
            })

    db.commit()
    logger.info(f"[AutoFiler] batch {batch_id}: filed={stats['filed']}, suggestions={len(stats['suggestions'])}, skipped={stats['skipped']}")
    return stats


def undo_batch(db: Session, batch_id: str) -> int:
    """撤销一批自动归档操作。返回撤销条数。"""
    actions = (
        db.query(KnowledgeFilingAction)
        .filter(
            KnowledgeFilingAction.batch_id == batch_id,
            KnowledgeFilingAction.action_type == "auto_file",
        )
        .all()
    )

    count = 0
    for action in actions:
        entry = db.get(KnowledgeEntry, action.knowledge_id)
        if not entry:
            continue
        # 只撤销 folder_id 还是当初设置的目标值的
        if entry.folder_id == action.to_folder_id:
            entry.folder_id = action.from_folder_id
            # 记录撤销操作
            undo_action = KnowledgeFilingAction(
                knowledge_id=entry.id,
                action_type="undo_auto_file",
                from_folder_id=action.to_folder_id,
                to_folder_id=action.from_folder_id,
                decision_source="manual",
                reason=f"撤销 batch {batch_id}",
                batch_id=batch_id,
                created_by=action.created_by,
            )
            db.add(undo_action)
            count += 1

    db.commit()
    logger.info(f"[AutoFiler] undo batch {batch_id}: {count} entries reverted")
    return count


def undo_single(db: Session, action_id: int) -> bool:
    """撤销单条自动归档。"""
    action = db.get(KnowledgeFilingAction, action_id)
    if not action or action.action_type != "auto_file":
        return False

    entry = db.get(KnowledgeEntry, action.knowledge_id)
    if not entry or entry.folder_id != action.to_folder_id:
        return False

    entry.folder_id = action.from_folder_id
    undo = KnowledgeFilingAction(
        knowledge_id=entry.id,
        action_type="undo_auto_file",
        from_folder_id=action.to_folder_id,
        to_folder_id=action.from_folder_id,
        decision_source="manual",
        reason=f"撤销 action #{action_id}",
        created_by=action.created_by,
    )
    db.add(undo)
    db.commit()
    return True


def get_unfiled_entries(db: Session, limit: int = 200) -> list[dict]:
    """获取所有未归档文档摘要。"""
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.folder_id.is_(None))
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "title": e.ai_title or e.title,
            "taxonomy_board": e.taxonomy_board,
            "taxonomy_code": e.taxonomy_code,
            "classification_status": e.classification_status,
            "source_type": e.source_type,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]


def get_filing_actions(
    db: Session,
    batch_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """获取归档操作记录。"""
    q = db.query(KnowledgeFilingAction)
    if batch_id:
        q = q.filter(KnowledgeFilingAction.batch_id == batch_id)
    actions = q.order_by(KnowledgeFilingAction.created_at.desc()).limit(limit).all()
    return [
        {
            "id": a.id,
            "knowledge_id": a.knowledge_id,
            "action_type": a.action_type,
            "from_folder_id": a.from_folder_id,
            "to_folder_id": a.to_folder_id,
            "decision_source": a.decision_source,
            "confidence": a.confidence,
            "reason": a.reason,
            "batch_id": a.batch_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in actions
    ]
