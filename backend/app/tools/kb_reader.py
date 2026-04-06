"""Knowledge base reader builtin tool.

Searches the knowledge base by keyword and returns matching entries' content.
Useful for skills that need to read uploaded documents like job descriptions,
monthly goals, policy files, etc.

Input params:
{
  "query": "岗位说明书 产品经理",
  "top_k": 3,          // optional, default 3, max 10
  "category": "hr"     // optional, filter by category
}

Output:
{
  "results": [
    {"title": "产品经理岗位说明书", "content": "...", "category": "hr"},
    ...
  ],
  "count": 2
}
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def execute(params: dict, db=None, user_id: int | None = None) -> dict:
    """Search knowledge base and return matching entries."""
    query = (params.get("query") or "").strip()
    top_k = min(int(params.get("top_k", 3)), 10)
    category = params.get("category")

    if not query:
        return {"results": [], "count": 0, "error": "query 不能为空"}

    if db is None:
        return {"results": [], "count": 0, "error": "数据库未注入"}

    results = []

    # 1. 优先走向量检索
    try:
        from app.services.vector_service import search_knowledge
        hits = search_knowledge(query, top_k=top_k * 2)  # 多取一些再过滤
        if hits:
            from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
            seen_ids = set()

            # 批量预加载脱敏 profile，避免 N+1
            kid_set = {h.get("knowledge_id") for h in hits if h.get("knowledge_id")}
            profile_map: dict[int, tuple[str, list]] = {}
            try:
                from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
                profiles = (
                    db.query(KnowledgeUnderstandingProfile)
                    .filter(KnowledgeUnderstandingProfile.knowledge_id.in_(kid_set))
                    .all()
                )
                for p in profiles:
                    profile_map[p.knowledge_id] = (
                        p.desensitization_level or "D1",
                        p.data_type_hits or [],
                    )
            except Exception:
                pass

            for h in hits:
                kid = h.get("knowledge_id")
                if not kid or kid in seen_ids:
                    continue
                entry = db.get(KnowledgeEntry, kid)
                if not entry:
                    continue
                if entry.status not in (KnowledgeStatus.APPROVED, KnowledgeStatus.AUTO_APPROVED):
                    continue
                if category and entry.category != category:
                    continue
                seen_ids.add(kid)

                raw_content = entry.content[:2000]
                content = _apply_desensitization(
                    raw_content, kid, user_id, entry.created_by, profile_map
                )

                results.append({
                    "title": entry.title,
                    "content": content,
                    "category": entry.category,
                    "score": round(h.get("score", 0), 3),
                })
                if len(results) >= top_k:
                    break
    except Exception as e:
        logger.warning(f"Vector search failed, falling back to SQL LIKE: {e}")

    # 2. 向量检索失败或无结果，降级到 SQL LIKE
    if not results:
        try:
            from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
            q = db.query(KnowledgeEntry).filter(
                KnowledgeEntry.status.in_([
                    KnowledgeStatus.APPROVED,
                    KnowledgeStatus.AUTO_APPROVED,
                ]),
                KnowledgeEntry.content.ilike(f"%{query}%") |
                KnowledgeEntry.title.ilike(f"%{query}%"),
            )
            if category:
                q = q.filter(KnowledgeEntry.category == category)
            entries = q.limit(top_k).all()

            # 批量预加载脱敏 profile
            kid_set = {e.id for e in entries}
            profile_map: dict[int, tuple[str, list]] = {}
            try:
                from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
                profiles = (
                    db.query(KnowledgeUnderstandingProfile)
                    .filter(KnowledgeUnderstandingProfile.knowledge_id.in_(kid_set))
                    .all()
                )
                for p in profiles:
                    profile_map[p.knowledge_id] = (
                        p.desensitization_level or "D1",
                        p.data_type_hits or [],
                    )
            except Exception:
                pass

            for entry in entries:
                raw_content = entry.content[:2000]
                content = _apply_desensitization(
                    raw_content, entry.id, user_id, entry.created_by, profile_map
                )
                results.append({
                    "title": entry.title,
                    "content": content,
                    "category": entry.category,
                    "score": None,
                })
        except Exception as e:
            logger.error(f"SQL fallback search failed: {e}")
            return {"results": [], "count": 0, "error": str(e)}

    return {"results": results, "count": len(results)}


def _apply_desensitization(
    raw_content: str,
    knowledge_id: int,
    user_id: int | None,
    created_by: int | None,
    profile_map: dict[int, tuple[str, list]],
) -> str:
    """按 D0-D4 脱敏分级处理内容。"""
    desens_level, data_type_hits = profile_map.get(knowledge_id, ("D1", []))
    is_own = user_id and created_by == user_id

    if is_own or desens_level == "D0":
        return raw_content
    if desens_level <= "D1":
        return raw_content

    # D2+ 脱敏
    try:
        from app.services.text_masker import mask_text
        content, _ = mask_text(raw_content, level=desens_level,
                               data_type_hits=data_type_hits or None)
        return content
    except Exception:
        return raw_content  # 脱敏失败降级
