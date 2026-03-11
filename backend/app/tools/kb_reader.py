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


def execute(params: dict, db=None) -> dict:
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
                results.append({
                    "title": entry.title,
                    "content": entry.content[:2000],  # 截断防止 context 过长
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
            for entry in entries:
                results.append({
                    "title": entry.title,
                    "content": entry.content[:2000],
                    "category": entry.category,
                    "score": None,
                })
        except Exception as e:
            logger.error(f"SQL fallback search failed: {e}")
            return {"results": [], "count": 0, "error": str(e)}

    return {"results": results, "count": len(results)}
