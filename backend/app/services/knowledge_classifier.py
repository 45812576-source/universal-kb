"""
知识分类器: 两阶段策略
  Stage 1: 关键词快速匹配（零 LLM 调用）
  Stage 2: LLM 精确分类（仅在 Stage 1 无法确定时）
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.data.knowledge_taxonomy import (
    TAXONOMY,
    KB_ID_DESCRIPTIONS,
    get_board_summary,
    keyword_search,
)
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

# Stage 1 的置信度阈值：命中关键词 >= 此值则跳过 LLM
_KEYWORD_CONFIDENT_THRESHOLD = 3

_CLASSIFY_PROMPT = """你是企业知识管理系统的分类助手，负责将工作文档/知识条目分配到正确的知识目录节点。

## 公司知识体系大板块
{board_summary}

## 候选分类节点（由关键词初步匹配得到）
{candidates_json}

## 所有知识库 ID 说明
{kb_ids_json}

## 待分类内容（前2000字）
{content}

---
请从候选节点中选择最匹配的节点，并输出分类结果。
如果候选节点都不合适，可从大板块中重新判断。

严格返回以下 JSON，不含其他任何内容:
{{
  "taxonomy_code": "A1.1",
  "taxonomy_board": "A",
  "taxonomy_path": ["A.渠道与平台", "A1.国内付费渠道", "A1.1.抖音/巨量引擎"],
  "storage_layer": "L2",
  "target_kb_ids": ["KT-01", "DB-08"],
  "serving_skill_codes": ["S04", "S11"],
  "reasoning": "内容涉及抖音巨量引擎的ECPM优化，属于渠道平台知识，应归入A1.1",
  "confidence": 0.92
}}"""


class ClassificationResult:
    """分类结果数据类。"""

    def __init__(
        self,
        taxonomy_code: str,
        taxonomy_board: str,
        taxonomy_path: list[str],
        storage_layer: str,
        target_kb_ids: list[str],
        serving_skill_codes: list[str],
        reasoning: str,
        confidence: float,
        stage: str,  # "keyword" or "llm"
    ):
        self.taxonomy_code = taxonomy_code
        self.taxonomy_board = taxonomy_board
        self.taxonomy_path = taxonomy_path
        self.storage_layer = storage_layer
        self.target_kb_ids = target_kb_ids
        self.serving_skill_codes = serving_skill_codes
        self.reasoning = reasoning
        self.confidence = confidence
        self.stage = stage

    def to_dict(self) -> dict:
        return {
            "taxonomy_code": self.taxonomy_code,
            "taxonomy_board": self.taxonomy_board,
            "taxonomy_path": self.taxonomy_path,
            "storage_layer": self.storage_layer,
            "target_kb_ids": self.target_kb_ids,
            "serving_skill_codes": self.serving_skill_codes,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "stage": self.stage,
        }


async def classify(text: str, db: Session) -> ClassificationResult | None:
    """
    对文本进行知识分类。

    Args:
        text: 待分类的文本内容
        db: 数据库 Session（用于获取 LLM 配置）

    Returns:
        ClassificationResult 或 None（无法分类时）
    """
    if not text or not text.strip():
        return None

    # Stage 1: 关键词快速匹配
    candidates = keyword_search(text, top_k=5)

    if candidates:
        top = candidates[0]
        top_score = top["score"]
        top_node = top["node"]

        if top_score >= _KEYWORD_CONFIDENT_THRESHOLD:
            # 关键词命中足够多，直接确定
            logger.debug(
                f"[Classifier] Stage 1 confident: {top_node['code']} (score={top_score})"
            )
            confidence = min(0.5 + top_score * 0.1, 0.85)
            return ClassificationResult(
                taxonomy_code=top_node["code"],
                taxonomy_board=top_node["board"],
                taxonomy_path=top_node["path"],
                storage_layer=top_node["layer"],
                target_kb_ids=top_node["kb_ids"],
                serving_skill_codes=top_node["serving_skills"],
                reasoning=f"关键词匹配（命中 {top_score} 个关键词）",
                confidence=confidence,
                stage="keyword",
            )

    # Stage 1.5: 向量候选辅助 —— 用 embedding 检索相似已分类文档，汇总高频 taxonomy
    vector_candidates = _get_vector_candidates(text, db)

    # Stage 2: LLM 精确分类（注入向量候选）
    try:
        stage = "vector_assisted_llm" if vector_candidates else "llm"
        result = await _llm_classify(text, candidates, db, vector_candidates=vector_candidates)
        if result:
            result.stage = stage
        return result
    except Exception as e:
        logger.warning(f"[Classifier] LLM classification failed: {e}")
        # LLM 失败时，若有关键词候选则降级使用
        if candidates:
            top_node = candidates[0]["node"]
            return ClassificationResult(
                taxonomy_code=top_node["code"],
                taxonomy_board=top_node["board"],
                taxonomy_path=top_node["path"],
                storage_layer=top_node["layer"],
                target_kb_ids=top_node["kb_ids"],
                serving_skill_codes=top_node["serving_skills"],
                reasoning=f"关键词匹配（LLM 降级）",
                confidence=0.3,
                stage="keyword_fallback",
            )
        return None


def _get_vector_candidates(text: str, db: Session) -> list[dict]:
    """用 embedding 检索相似已分类知识条目，汇总高频 taxonomy 作为候选。"""
    try:
        from app.services import vector_service
        hits = vector_service.search_knowledge(text[:500], top_k=10)
    except Exception as e:
        logger.debug(f"[Classifier] vector search failed, skipping: {e}")
        return []

    if not hits:
        return []

    # 从 hits 中拿到 knowledge_id，查询已分类的条目
    from app.models.knowledge import KnowledgeEntry
    kid_set = {h["knowledge_id"] for h in hits if h.get("knowledge_id")}
    if not kid_set:
        return []

    entries = (
        db.query(
            KnowledgeEntry.taxonomy_code,
            KnowledgeEntry.taxonomy_board,
            KnowledgeEntry.taxonomy_path,
        )
        .filter(
            KnowledgeEntry.id.in_(kid_set),
            KnowledgeEntry.taxonomy_code.isnot(None),
        )
        .all()
    )

    if not entries:
        return []

    # 统计高频 taxonomy_code
    from collections import Counter
    code_counter = Counter()
    code_info: dict[str, dict] = {}
    for e in entries:
        code_counter[e.taxonomy_code] += 1
        if e.taxonomy_code not in code_info:
            code_info[e.taxonomy_code] = {
                "taxonomy_code": e.taxonomy_code,
                "taxonomy_board": e.taxonomy_board,
                "taxonomy_path": e.taxonomy_path or [],
            }

    # 返回出现次数最多的前 5 个
    result = []
    for code, count in code_counter.most_common(5):
        info = code_info[code]
        info["similar_count"] = count
        result.append(info)

    logger.debug(f"[Classifier] vector candidates: {[r['taxonomy_code'] for r in result]}")
    return result


async def _llm_classify(
    text: str,
    candidates: list[dict],
    db: Session,
    vector_candidates: list[dict] | None = None,
) -> ClassificationResult | None:
    """Stage 2: 调用 LLM 精确分类。"""
    # 构建候选节点的简洁描述（避免 prompt 过长）
    candidate_nodes = []
    for c in candidates:
        node = c["node"]
        candidate_nodes.append({
            "code": node["code"],
            "name": node["name"],
            "board": node["board"],
            "path": node["path"],
            "layer": node["layer"],
            "kb_ids": node["kb_ids"],
            "serving_skills": node["serving_skills"],
            "match_score": c["score"],
        })

    # 若无关键词候选，注入所有节点的简要列表
    if not candidate_nodes:
        candidate_nodes = [
            {
                "code": n["code"],
                "name": n["name"],
                "board": n["board"],
                "path": n["path"][:2],
                "layer": n["layer"],
                "kb_ids": n["kb_ids"],
                "serving_skills": n["serving_skills"],
                "match_score": 0,
            }
            for n in TAXONOMY
        ]

    # 注入向量候选信息
    vector_hint = ""
    if vector_candidates:
        vector_hint = "\n\n## 相似文档的分类参考（通过向量检索获得）\n"
        for vc in vector_candidates:
            vector_hint += f"- {vc['taxonomy_code']} ({' > '.join(vc.get('taxonomy_path', []))}) — 相似文档 {vc['similar_count']} 篇\n"
        vector_hint += "\n以上是与待分类文档语义相似的已分类文档所属分类，仅作参考。\n"

    prompt = _CLASSIFY_PROMPT.format(
        board_summary=get_board_summary(),
        candidates_json=json.dumps(candidate_nodes, ensure_ascii=False, indent=2),
        kb_ids_json=json.dumps(KB_ID_DESCRIPTIONS, ensure_ascii=False, indent=2),
        content=text[:2000],
    )
    if vector_hint:
        prompt = prompt.replace("---\n请从候选节点中", f"{vector_hint}---\n请从候选节点中")

    model_config = llm_gateway.resolve_config(db, "knowledge.classify")
    result_str, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=800,
    )

    # 清理 markdown 代码块
    result_str = result_str.strip()
    if result_str.startswith("```"):
        result_str = result_str.split("```")[1]
        if result_str.startswith("json"):
            result_str = result_str[4:]
    result_str = result_str.strip()

    parsed = json.loads(result_str)

    # 从 taxonomy 中找到对应节点补充信息（LLM 可能只返回 code）
    node_by_code = {n["code"]: n for n in TAXONOMY}
    matched_node = node_by_code.get(parsed.get("taxonomy_code", ""))

    return ClassificationResult(
        taxonomy_code=parsed.get("taxonomy_code", ""),
        taxonomy_board=parsed.get("taxonomy_board", matched_node["board"] if matched_node else ""),
        taxonomy_path=parsed.get("taxonomy_path", matched_node["path"] if matched_node else []),
        storage_layer=parsed.get("storage_layer", matched_node["layer"] if matched_node else "L2"),
        target_kb_ids=parsed.get("target_kb_ids", matched_node["kb_ids"] if matched_node else []),
        serving_skill_codes=parsed.get("serving_skill_codes", matched_node["serving_skills"] if matched_node else []),
        reasoning=parsed.get("reasoning", ""),
        confidence=float(parsed.get("confidence", 0.7)),
        stage="llm",
    )


def apply_classification_to_entry(entry, result: ClassificationResult) -> None:
    """将分类结果写入 KnowledgeEntry 对象（不 commit）。"""
    entry.taxonomy_code = result.taxonomy_code
    entry.taxonomy_board = result.taxonomy_board
    entry.taxonomy_path = result.taxonomy_path
    entry.storage_layer = result.storage_layer
    entry.target_kb_ids = result.target_kb_ids
    entry.serving_skill_codes = result.serving_skill_codes
    entry.ai_classification_note = result.reasoning
    entry.classification_confidence = result.confidence
