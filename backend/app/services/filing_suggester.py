"""归档建议服务：基于分类结果和相似文档分布为未归档文档推荐 folder。"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_filing import KnowledgeFilingSuggestion

logger = logging.getLogger(__name__)


def _build_folder_path_map(db: Session, user_id: int) -> dict[int, str]:
    """构建 folder_id → 完整路径 的映射。"""
    folders = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.created_by == user_id
    ).all()
    id_to_folder = {f.id: f for f in folders}
    path_cache: dict[int, str] = {}

    def _path(fid: int) -> str:
        if fid in path_cache:
            return path_cache[fid]
        f = id_to_folder.get(fid)
        if not f:
            return ""
        if f.parent_id and f.parent_id in id_to_folder:
            parent_path = _path(f.parent_id)
            full = f"{parent_path}/{f.name}" if parent_path else f.name
        else:
            full = f.name
        path_cache[fid] = full
        return full

    for f in folders:
        _path(f.id)
    return path_cache


def _suggest_folder_for_entry(
    db: Session,
    entry: KnowledgeEntry,
    folder_path_map: dict[int, str],
) -> Optional[dict]:
    """为单条未归档文档生成归档建议。

    策略：
    1. 同 taxonomy_board + taxonomy_code 的已归档文档，取最高频 folder
    2. 同 taxonomy_board 的已归档文档，取最高频 folder
    3. 无建议
    """
    if not folder_path_map:
        return None

    # 策略1：精确分类匹配
    confidence = 0.0
    reason = ""
    suggested_folder_id = None

    if entry.taxonomy_code:
        same_code = (
            db.query(KnowledgeEntry.folder_id)
            .filter(
                KnowledgeEntry.taxonomy_code == entry.taxonomy_code,
                KnowledgeEntry.folder_id.isnot(None),
                KnowledgeEntry.id != entry.id,
            )
            .limit(50)
            .all()
        )
        if same_code:
            counter = Counter(fid for (fid,) in same_code if fid)
            if counter:
                top_fid, top_count = counter.most_common(1)[0]
                confidence = min(0.95, top_count / len(same_code))
                suggested_folder_id = top_fid
                reason = f"同分类 {entry.taxonomy_code} 下 {top_count}/{len(same_code)} 篇文档归于此"

    # 策略2：大板块匹配
    if not suggested_folder_id and entry.taxonomy_board:
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
                confidence = min(0.7, top_count / len(same_board))
                suggested_folder_id = top_fid
                reason = f"同板块 {entry.taxonomy_board} 下 {top_count}/{len(same_board)} 篇文档归于此"

    if not suggested_folder_id:
        return None

    return {
        "suggested_folder_id": suggested_folder_id,
        "suggested_folder_path": folder_path_map.get(suggested_folder_id, ""),
        "confidence": round(confidence, 2),
        "reason": reason,
        "based_on": {
            "taxonomy_code": entry.taxonomy_code,
            "taxonomy_board": entry.taxonomy_board,
        },
    }


async def suggest_folders_batch(
    db: Session,
    entry_ids: list[int],
    user_id: int,
) -> list[dict]:
    """批量为未归档文档生成归档建议，写入 knowledge_filing_suggestions 表。"""
    folder_path_map = _build_folder_path_map(db, user_id)

    results = []
    for eid in entry_ids:
        entry = db.get(KnowledgeEntry, eid)
        if not entry:
            results.append({"knowledge_id": eid, "suggestion": None, "error": "not found"})
            continue

        suggestion_data = _suggest_folder_for_entry(db, entry, folder_path_map)
        if not suggestion_data:
            results.append({"knowledge_id": eid, "suggestion": None})
            continue

        # 写入建议表
        s = KnowledgeFilingSuggestion(
            knowledge_id=eid,
            suggested_folder_id=suggestion_data["suggested_folder_id"],
            suggested_folder_path=suggestion_data["suggested_folder_path"],
            confidence=suggestion_data["confidence"],
            reason=suggestion_data["reason"],
            based_on=suggestion_data["based_on"],
            status="pending",
        )
        db.add(s)
        db.flush()

        results.append({
            "knowledge_id": eid,
            "suggestion": {
                "id": s.id,
                "suggested_folder_id": s.suggested_folder_id,
                "suggested_folder_path": s.suggested_folder_path,
                "confidence": s.confidence,
                "reason": s.reason,
            },
        })

    db.commit()
    return results
