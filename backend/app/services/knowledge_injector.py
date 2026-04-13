"""KnowledgeInjector — 知识检索与注入，从 skill_engine 抽出。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import Skill
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)


class KnowledgeInjector:
    """知识检索、精排与注入。"""

    async def rerank_hits_with_llm(
        self,
        db: Session,
        query: str,
        hits: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """用 DeepSeek lite 从候选 chunks 中筛选出最相关的 top_k 条。"""
        if len(hits) <= top_k:
            return hits
        snippets = "\n".join(
            f"[{i}] {h.get('title', '')}：{h['text'][:150]}"
            for i, h in enumerate(hits)
        )
        prompt = (
            f"用户问题：{query}\n\n候选知识片段：\n{snippets}\n\n"
            f"请从中选出与用户问题最相关的 {top_k} 条，返回序号（逗号分隔），只返回数字。"
        )
        try:
            result, _ = await llm_gateway.chat(
                model_config=llm_gateway.resolve_config(db, "skill.rerank"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=50,
            )
            indices = [int(x.strip()) for x in result.split(",") if x.strip().isdigit()]
            selected = [hits[i] for i in indices if i < len(hits)]
            return selected if selected else hits[:top_k]
        except Exception as e:
            logger.warning(f"Knowledge rerank failed, using top-{top_k} raw: {e}")
            return hits[:top_k]

    async def inject_knowledge(
        self,
        query: str,
        skill: Skill | None,
        db=None,
        user_id: int | None = None,
        project_id: int | None = None,
        rerank_fn=None,
    ) -> str:
        """Retrieve relevant knowledge chunks from Milvus and format as context.

        Access control（按脱敏级别动态决定）：
        - 自己创建 or D0 → 原文注入
        - 已审批 + D1以下 → 原文注入
        - 其余 → 按文档脱敏级别动态脱敏后注入

        二阶段召回：粗召回 top_20 → LLM 精排 top_5
        """
        try:
            from app.services.vector_service import search_knowledge
            search_kwargs = {"query": query, "top_k": 20}

            if skill and hasattr(skill, "taxonomy_board") and skill.taxonomy_board:
                search_kwargs["taxonomy_board"] = skill.taxonomy_board

            search_kwargs["min_quality"] = 0.3

            hits = await asyncio.wait_for(
                asyncio.to_thread(lambda: search_knowledge(**search_kwargs)),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Knowledge search timed out after 3s, skipping")
            return ""
        except Exception as e:
            logger.warning(f"Knowledge search failed: {e}")
            return ""

        if not hits:
            return ""

        # 条件精排
        if len(hits) <= 5:
            pass
        elif len(hits) >= 5 and all(h.get("score", 0) > 0.75 for h in hits[:5]):
            hits = hits[:5]
        else:
            reranker = rerank_fn or self.rerank_hits_with_llm
            hits = await reranker(db, query, hits, top_k=5)

        # 查询已审批 knowledge_ids
        from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
        approved_ids: set[int] = set()
        if db:
            try:
                approved_entries = (
                    db.query(KnowledgeEntry.id)
                    .filter(KnowledgeEntry.status == KnowledgeStatus.APPROVED)
                    .all()
                )
                approved_ids = {row[0] for row in approved_entries}
            except Exception as e:
                logger.warning(f"Failed to load approved knowledge ids: {e}")

            if project_id:
                try:
                    from app.models.project import ProjectKnowledgeShare
                    project_kb_ids = {
                        row[0] for row in
                        db.query(ProjectKnowledgeShare.knowledge_id)
                        .filter(ProjectKnowledgeShare.project_id == project_id)
                        .all()
                    }
                    approved_ids |= project_kb_ids
                except Exception as e:
                    logger.warning(f"Failed to load project knowledge ids: {e}")

        # 批量预查文档脱敏级别
        doc_levels: dict[int, str] = {}
        doc_data_type_hits: dict[int, list[dict]] = {}
        published_ref_ids: set[int] | None = None
        kid_set = {h["knowledge_id"] for h in hits}

        if db:
            try:
                from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
                profiles = (
                    db.query(KnowledgeUnderstandingProfile)
                    .filter(KnowledgeUnderstandingProfile.knowledge_id.in_(kid_set))
                    .all()
                )
                for p in profiles:
                    doc_levels[p.knowledge_id] = p.desensitization_level or "D1"
                    doc_data_type_hits[p.knowledge_id] = p.data_type_hits or []
            except Exception as e:
                logger.warning(f"Failed to load doc desensitization levels: {e}")

            # 批量加载 visibility_scope 和 created_by
            kb_scope_map: dict[int, tuple[str | None, int]] = {}
            try:
                scope_rows = db.query(
                    KnowledgeEntry.id, KnowledgeEntry.visibility_scope, KnowledgeEntry.created_by
                ).filter(KnowledgeEntry.id.in_(kid_set)).all()
                kb_scope_map = {r.id: (r.visibility_scope, r.created_by) for r in scope_rows}
            except Exception:
                pass

            # 已发布 Skill：加载已审知识引用集合
            if skill and hasattr(skill, 'status') and str(getattr(skill.status, 'value', skill.status)) == 'published':
                try:
                    from app.models.skill_knowledge_ref import SkillKnowledgeReference
                    published_ref_ids = {
                        r.knowledge_id for r in
                        db.query(SkillKnowledgeReference.knowledge_id)
                        .filter(SkillKnowledgeReference.skill_id == skill.id)
                        .all()
                    }
                except Exception as e:
                    logger.warning(f"Failed to load published skill knowledge refs: {e}")

        parts = []
        seen_ids: set[int] = set()
        for h in hits:
            kid = h["knowledge_id"]
            if kid in seen_ids:
                continue
            seen_ids.add(kid)

            # visibility_scope 检查
            scope_info = kb_scope_map.get(kid) if db else None
            if scope_info:
                vs, vs_creator = scope_info
                if vs == "project" and not project_id:
                    continue
                if vs in ("owner_only", "private") and vs_creator != user_id:
                    continue

            # 已发布 Skill 运行时召回约束
            if published_ref_ids is not None and kid not in published_ref_ids:
                doc_level = doc_levels.get(kid, "D1")
                if doc_level >= "D2":
                    logger.info(f"Skill {skill.id}: 跳过未审知识 {kid}（{doc_level}）")
                    continue

            chunk_owner = h.get("created_by", 0)
            is_own = user_id and chunk_owner == user_id
            is_approved = kid in approved_ids
            doc_level = doc_levels.get(kid, "D1")

            if is_own or doc_level == "D0":
                parts.append(f"[相关知识]\n{h['text']}")
            elif is_approved and doc_level <= "D1":
                parts.append(f"[相关知识]\n{h['text']}")
            else:
                masked_text = h.get("desensitized_text", "").strip()
                if not masked_text:
                    try:
                        from app.services.text_masker import mask_text
                        type_hits = doc_data_type_hits.get(kid)
                        result_text, replacements = mask_text(h["text"], level=doc_level, data_type_hits=type_hits)
                        if replacements:
                            masked_text = result_text
                    except Exception:
                        pass
                if not masked_text:
                    from app.services.vector_service import _desensitize_rule
                    masked_text = _desensitize_rule(h["text"])
                if masked_text:
                    parts.append(f"[参考知识（已脱敏）]\n{masked_text}")

        return "\n\n---\n\n".join(parts)


knowledge_injector = KnowledgeInjector()
