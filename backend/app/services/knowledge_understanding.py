"""统一文档理解流水线：一次性产出标题、分类与权限标签、内容标签(5维)、
摘要(短+搜索)、数据类型识别、脱敏级别，并统一落库。

8 步串行，步骤 2/3/4 纯规则零 LLM，步骤 5/6/7 合并为一次 LLM 调用。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re

from sqlalchemy.orm import Session

from app.data.sensitivity_rules import (
    CONTENT_TAG_VOCABULARY,
    DOCUMENT_TYPES,
    PERMISSION_DOMAINS,
    check_taxonomy_doctype_conflict,
    compute_desensitization_level,
    detect_data_types,
    generate_system_id,
    get_summary_sensitivity_mode,
    infer_document_type,
    validate_content_tags,
)
from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
from app.services.llm_gateway import SLOT_REGISTRY, llm_gateway

logger = logging.getLogger(__name__)

# 注册 LLM slot
SLOT_REGISTRY["knowledge.understand"] = {
    "name": "文档理解",
    "category": "知识",
    "desc": "统一文档理解流水线：自动命名+分类+标签+摘要",
    "fallback": "lite",
}

# ── 合并 Prompt（步骤 5/6/7 合为一次调用）─────────────────────────────────────

_UNDERSTAND_PROMPT = """你是企业知识管理系统的文档理解助手。请根据文档信息一次性产出以下结构化元数据。

## 文件信息
- 原始文件名: {filename}
- 文件类型: {file_type}
- 规则层已识别的文档类型: {rule_doc_type}
- 规则层已识别的数据类型命中: {data_type_hits}
- 脱敏级别: {desensitization_level}
- 摘要脱敏模式: {summary_mode}

## 文档内容
{content}

---
请严格返回以下 JSON，不含其他任何内容:
{{
  "title": "简明的中文标题（≤50字，描述核心主题）",
  "title_confidence": 0.85,
  "title_reason": "标题来源推理说明",
  "document_type": "从以下枚举中选择最匹配的: {doc_type_list}",
  "permission_domain": "从以下枚举中选择: {perm_domain_list}",
  "content_tags": {{
    "subject_tag": {{"value": "谁产出/使用（从词表选或新建）", "confidence": 0.9}},
    "object_tag": {{"value": "涉及什么对象", "confidence": 0.85}},
    "scenario_tag": {{"value": "什么场景下使用", "confidence": 0.8}},
    "action_tag": {{"value": "做什么动作", "confidence": 0.8}},
    "industry_or_domain_tag": {{"value": "属于什么行业/领域", "confidence": 0.7}}
  }},
  "suggested_tags": ["自由标签1", "自由标签2"],
  "summary_short": "≤50字的核心摘要",
  "summary_search": "≤300字的检索用摘要（{summary_instruction}）",
  "summary_embedding": "≤300字的向量检索专用摘要（不做任何脱敏，实体展开全称，包含同义词和关键术语，便于语义匹配）",
  "data_type_validation": [],
  "quality_score": 0.85
}}

## summary_short 生成规则（≤50字，最重要）:
- 目标：让从未读过此文档的人一眼判断"要不要打开看"
- 公式：[主体] + [做了什么/关于什么] + [核心结论/数据点]
- 必须保留原文中的关键数据点（数字、百分比、金额），不得抽象为"有所提升"等模糊表述
- 好例子："抖音美妆Q1投放ROI从1.2提升至2.8，核心策略是短视频+直播联投"
- 坏例子："关于抖音投放策略的分析报告"（太泛，无信息增量）
- 脱敏模式 {summary_mode}: {summary_instruction}

## summary_search 生成规则（≤300字）:
- 覆盖文档中所有关键实体、方法论名称、数据结论
- 保留原文数据点，不做概括性改写
- 脱敏模式同 summary_short

## summary_embedding 生成规则（≤300字）:
- 始终不脱敏（raw），仅用于向量引擎，不对人展示
- 实体展开全称，包含同义词和关键术语

## content_tags 规则:
- 每个维度必须带 value 和 confidence（0-1）
- confidence < 0.5 的维度请填词表中的 fallback 值

## data_type_validation 规则:
- 仅当规则层识别到数据类型命中时需要填写
- 对每个规则层命中进行校验，格式: [{{"type": "passport_number", "rule_hit": true, "actually_present": true/false, "reason": "说明"}}]
- 若规则层无命中，返回空数组 []

## 质量评分标准（0-1）:
- 0.9+: 有独特方法论/数据洞察/完整案例
- 0.7-0.9: 有价值的经验总结或操作指南
- 0.5-0.7: 一般性信息
- <0.5: 碎片化或无效内容"""

# ── 长文档 Map-Reduce 摘要 Prompts ────────────────────────────────────────────

# 长文档阈值（字符数），超过此阈值走 map-reduce 两阶段
_LONG_DOC_THRESHOLD = 3000

_CHUNK_MAP_PROMPT = """你是企业知识管理系统的摘要助手。请对以下文档片段提取关键信息。

## 要求
- 保留原文中的具体数据点（数字、百分比、金额、日期），不得模糊化
- 保留关键实体名称（人名、公司名、产品名、平台名）
- 保留核心结论和因果关系
- 输出 100-200 字，纯文本，不要 markdown 格式

## 文档片段（第 {chunk_idx}/{total_chunks} 段）
{chunk_text}

---
请直接输出摘要文本，不要任何前缀或解释："""

_REDUCE_PROMPT = """你是企业知识管理系统的摘要助手。以下是一篇长文档各段落的摘要，请合并为最终结构化摘要。

## 各段摘要
{chunk_summaries}

## 脱敏模式: {summary_mode}
{summary_instruction}

---
请严格返回以下 JSON，不含其他任何内容:
{{
  "summary_short": "≤50字的核心摘要",
  "summary_search": "≤300字的检索用摘要",
  "summary_embedding": "≤300字的向量检索专用摘要（不脱敏，实体展开全称，含同义词）"
}}

## summary_short 规则（最重要）:
- 让从未读过此文档的人一眼判断"要不要打开看"
- 公式：[主体] + [做了什么/关于什么] + [核心结论/数据点]
- 必须保留原文关键数据点（数字、百分比、金额），禁止模糊化为"有所提升"
- 好例子："抖音美妆Q1投放ROI从1.2升至2.8，核心是短视频+直播联投"
- 坏例子："关于投放策略的分析报告"

## summary_search 规则:
- 覆盖全文所有关键实体、方法论、数据结论
- 保留原文数据点，不做概括性改写"""


async def understand_document(
    knowledge_id: int,
    content: str,
    filename: str,
    file_type: str,
    db: Session,
) -> KnowledgeUnderstandingProfile:
    """统一文档理解流水线入口。"""
    # 查找或创建 profile
    profile = (
        db.query(KnowledgeUnderstandingProfile)
        .filter(KnowledgeUnderstandingProfile.knowledge_id == knowledge_id)
        .first()
    )
    if not profile:
        profile = KnowledgeUnderstandingProfile(
            knowledge_id=knowledge_id,
            understanding_status="running",
        )
        db.add(profile)
        db.flush()
    else:
        profile.understanding_status = "running"
        profile.understanding_error = None
        profile.understanding_version = (profile.understanding_version or 0) + 1
        db.flush()

    errors: list[str] = []

    try:
        # ── Step 1: 原始文本（已由上传流程完成，此处接收 content）──────────
        profile.raw_title = filename

        # ── Step 2: 文档类型识别（纯规则）────────────────────────────────
        rule_doc_type, doc_type_source = infer_document_type(filename, content)
        profile.classification_source = doc_type_source

        # ── Step 3: 数据类型识别（正则 + 关键词）────────────────────────
        data_type_hits = detect_data_types(content)
        profile.data_type_hits = data_type_hits
        profile.contains_sensitive_data = len(data_type_hits) > 0

        # ── Step 4: 脱敏级别判定（规则）──────────────────────────────────
        desens_level, visibility_rec = compute_desensitization_level(
            data_type_hits, rule_doc_type
        )
        profile.desensitization_level = desens_level
        profile.visibility_recommendation = visibility_rec
        profile.masking_source = "rule"

        summary_mode = get_summary_sensitivity_mode(rule_doc_type, desens_level)
        profile.summary_sensitivity_mode = summary_mode

        # ── Steps 5/6/7: 合并 LLM 调用 ──────────────────────────────────
        if content and content.strip():
            try:
                llm_result = await _call_llm_understand(
                    content=content,
                    filename=filename,
                    file_type=file_type,
                    rule_doc_type=rule_doc_type,
                    data_type_hits=data_type_hits,
                    desensitization_level=desens_level,
                    summary_mode=summary_mode,
                    db=db,
                )
                _apply_llm_result(profile, llm_result, rule_doc_type)
                # LLM 校验可能剔除了误报的 data_type_hits，需重算脱敏级别
                if profile.data_type_hits != data_type_hits:
                    desens_level, visibility_rec = compute_desensitization_level(
                        profile.data_type_hits, profile.document_type or rule_doc_type
                    )
                    profile.desensitization_level = desens_level
                    profile.visibility_recommendation = visibility_rec
                    profile.masking_source = "llm_corrected"
            except Exception as e:
                logger.warning(f"[Understanding] LLM call failed for knowledge {knowledge_id}: {e}")
                errors.append(f"LLM: {e}")
                _apply_fallback(profile, filename, content, rule_doc_type)
        else:
            _apply_fallback(profile, filename, content, rule_doc_type)

        # ── Step 7.5: taxonomy ↔ document_type 冲突检查 ────────────────
        from app.models.knowledge import KnowledgeEntry
        entry = db.get(KnowledgeEntry, knowledge_id)
        if entry and entry.taxonomy_board and profile.document_type:
            conflict = check_taxonomy_doctype_conflict(
                entry.taxonomy_board, profile.document_type
            )
            if conflict:
                logger.info(
                    f"[Understanding] taxonomy/doctype conflict for knowledge {knowledge_id}: "
                    f"board={entry.taxonomy_board} doctype={profile.document_type}"
                )
                if not profile.understanding_error:
                    profile.understanding_error = conflict["message"]
                else:
                    profile.understanding_error += f"; {conflict['message']}"

        # ── Step 8: 生成系统编号 + 结果落库 ─────────────────────────────
        if not profile.system_id and profile.document_type:
            profile.system_id = generate_system_id(
                profile.document_type, knowledge_id
            )

        if errors:
            profile.understanding_status = "partial"
            profile.understanding_error = "; ".join(errors)
        else:
            profile.understanding_status = "success"
            profile.understanding_error = None

        profile.updated_at = datetime.datetime.utcnow()
        db.commit()

    except Exception as e:
        logger.exception(f"[Understanding] Pipeline failed for knowledge {knowledge_id}")
        profile.understanding_status = "failed"
        profile.understanding_error = str(e)[:500]
        profile.updated_at = datetime.datetime.utcnow()
        try:
            db.commit()
        except Exception:
            db.rollback()

    return profile


_SUMMARY_INSTRUCTIONS = {
    "raw": "正常生成，无需脱敏",
    "masked": "精确金额/手机号等敏感数据用***替代",
    "abstracted": "敏感实体用通用代称，如\"某客户\"\"某金额范围\"",
}


def _clean_llm_json(raw: str) -> str:
    """清理 LLM 返回的 markdown 代码块包装。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


async def _call_llm_understand(
    content: str,
    filename: str,
    file_type: str,
    rule_doc_type: str | None,
    data_type_hits: list[dict],
    desensitization_level: str,
    summary_mode: str,
    db: Session,
) -> dict:
    """统一 LLM 理解入口。短文档一次出；长文档走 map-reduce 两阶段摘要。"""
    is_long = len(content) > _LONG_DOC_THRESHOLD
    summary_instruction = _SUMMARY_INSTRUCTIONS.get(summary_mode, "正常生成")

    if is_long:
        # ── 长文档：先 map 各段摘要，再 reduce 合并 ──────────────────────
        chunk_summaries_text = await _map_chunk_summaries(content, db)

        # 主调用：用前 3000 字做分类/标签/命名，摘要字段留空让 reduce 填
        main_result = await _call_understand_core(
            content=content[:3000],
            filename=filename,
            file_type=file_type,
            rule_doc_type=rule_doc_type,
            data_type_hits=data_type_hits,
            desensitization_level=desensitization_level,
            summary_mode=summary_mode,
            summary_instruction=summary_instruction,
            db=db,
        )

        # reduce 调用：从段落摘要中压缩出最终 summary
        reduce_result = await _reduce_summaries(
            chunk_summaries_text, summary_mode, summary_instruction, db
        )
        main_result["summary_short"] = reduce_result.get("summary_short", "")
        main_result["summary_search"] = reduce_result.get("summary_search", "")
        main_result["summary_embedding"] = reduce_result.get("summary_embedding", "")
        return main_result
    else:
        # ── 短文档：一次调用全出 ─────────────────────────────────────────
        return await _call_understand_core(
            content=content,
            filename=filename,
            file_type=file_type,
            rule_doc_type=rule_doc_type,
            data_type_hits=data_type_hits,
            desensitization_level=desensitization_level,
            summary_mode=summary_mode,
            summary_instruction=summary_instruction,
            db=db,
        )


async def _call_understand_core(
    content: str,
    filename: str,
    file_type: str,
    rule_doc_type: str | None,
    data_type_hits: list[dict],
    desensitization_level: str,
    summary_mode: str,
    summary_instruction: str,
    db: Session,
) -> dict:
    """单次 LLM 调用，产出标题/分类/标签/摘要。"""
    prompt = _UNDERSTAND_PROMPT.format(
        filename=filename or "未知",
        file_type=file_type or "未知",
        rule_doc_type=rule_doc_type or "未识别（请从枚举中判断）",
        data_type_hits=json.dumps(
            [{"type": h["type"], "label": h["label"], "count": h["count"]} for h in data_type_hits],
            ensure_ascii=False,
        ) if data_type_hits else "无",
        desensitization_level=desensitization_level,
        summary_mode=summary_mode,
        content=content[:3000],
        doc_type_list=", ".join(DOCUMENT_TYPES.keys()),
        perm_domain_list=", ".join(PERMISSION_DOMAINS.keys()),
        summary_instruction=summary_instruction,
    )

    model_config = llm_gateway.resolve_config(db, "knowledge.understand")
    result_str, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1500,
    )

    return json.loads(_clean_llm_json(result_str))


async def _map_chunk_summaries(content: str, db: Session) -> str:
    """Map 阶段：将长文档分段，每段独立提取关键信息摘要。"""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=3000,
        chunk_overlap=300,
        separators=["\n\n", "\n", "。", "；", "，", " "],
    )
    chunks = splitter.split_text(content)

    model_config = llm_gateway.resolve_config(db, "knowledge.understand")
    summaries: list[str] = []

    for idx, chunk in enumerate(chunks, 1):
        prompt = _CHUNK_MAP_PROMPT.format(
            chunk_idx=idx,
            total_chunks=len(chunks),
            chunk_text=chunk,
        )
        result_str, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )
        summaries.append(result_str.strip())

    return "\n\n---\n\n".join(
        f"【第{i+1}段】{s}" for i, s in enumerate(summaries)
    )


async def _reduce_summaries(
    chunk_summaries_text: str,
    summary_mode: str,
    summary_instruction: str,
    db: Session,
) -> dict:
    """Reduce 阶段：从段落摘要中压缩出最终三层 summary。"""
    prompt = _REDUCE_PROMPT.format(
        chunk_summaries=chunk_summaries_text,
        summary_mode=summary_mode,
        summary_instruction=summary_instruction,
    )

    model_config = llm_gateway.resolve_config(db, "knowledge.understand")
    result_str, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1000,
    )

    return json.loads(_clean_llm_json(result_str))


def _apply_llm_result(
    profile: KnowledgeUnderstandingProfile,
    result: dict,
    rule_doc_type: str | None,
) -> None:
    """将 LLM 结果写入 profile，跳过人工修正过的字段。"""
    # 标题（人工修正优先）
    if profile.title_source != "user":
        profile.display_title = (result.get("title") or profile.raw_title or "未命名文档")[:500]
        profile.title_confidence = max(0.0, min(1.0, float(result.get("title_confidence", 0.7))))
        profile.title_source = "ai"
        profile.title_reason = result.get("title_reason", "")

    # 文档类型（人工修正优先）
    if profile.classification_source == "manual":
        pass  # 保留人工修正
    elif (llm_doc_type := result.get("document_type", "")) and llm_doc_type in DOCUMENT_TYPES:
        profile.document_type = llm_doc_type
        if rule_doc_type and rule_doc_type != llm_doc_type:
            profile.classification_source = "mixed"
        else:
            profile.classification_source = "llm"
    elif rule_doc_type:
        profile.document_type = rule_doc_type
        profile.classification_source = "rule"
    else:
        profile.document_type = "other"
        profile.classification_source = "fallback"

    # 权限域
    perm = result.get("permission_domain", "")
    profile.permission_domain = perm if perm in PERMISSION_DOMAINS else "department"

    # 5维内容标签（人工修正优先）
    if profile.tagging_source != "manual":
        raw_tags = result.get("content_tags", {})
        # 兼容新格式（带 confidence）和旧格式（纯字符串）
        flat_tags = {}
        confidences = {}
        for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
            val = raw_tags.get(dim)
            if isinstance(val, dict):
                flat_tags[dim] = val.get("value", "")
                confidences[dim] = max(0.0, min(1.0, float(val.get("confidence", 0.5))))
            else:
                flat_tags[dim] = val
                confidences[dim] = 0.5
        profile.content_tags = validate_content_tags(flat_tags)
        profile.content_tag_confidences = confidences
        profile.tagging_source = "llm"

    # 建议标签
    suggested = result.get("suggested_tags", [])
    if isinstance(suggested, list):
        profile.suggested_tags = suggested[:10]

    # 摘要
    profile.summary_short = (result.get("summary_short") or "")[:50]
    profile.summary_search = (result.get("summary_search") or "")[:500]
    profile.summary_embedding = (result.get("summary_embedding") or "")[:500]
    profile.summarization_source = "llm"

    # 数据类型校验（LLM 纠正规则层误报）
    validations = result.get("data_type_validation", [])
    if isinstance(validations, list) and validations:
        false_positives = [
            v["type"] for v in validations
            if isinstance(v, dict) and v.get("actually_present") is False
        ]
        if false_positives and profile.data_type_hits:
            profile.data_type_hits = [
                h for h in profile.data_type_hits
                if h.get("type") not in false_positives
            ]


def _apply_fallback(
    profile: KnowledgeUnderstandingProfile,
    filename: str,
    content: str,
    rule_doc_type: str | None,
) -> None:
    """LLM 调用失败时的降级处理。"""
    # 标题（内联清洗，避免从 router 层导入导致循环依赖）
    import os as _os
    clean_name = filename or "未命名文档"
    name_part, ext_part = _os.path.splitext(clean_name)
    if ext_part and len(ext_part) <= 6:
        clean_name = name_part
    clean_name = re.sub(r"[\x00-\x1f\x7f]", "", clean_name)
    clean_name = re.sub(r"\s+", " ", clean_name).strip() or "未命名文档"
    profile.display_title = clean_name
    profile.title_confidence = 0.3
    profile.title_source = "cleaned_filename" if filename else "fallback"
    profile.title_reason = "LLM 调用失败，使用文件名降级"

    # 文档类型
    profile.document_type = rule_doc_type or "other"
    profile.classification_source = "rule" if rule_doc_type else "fallback"

    # 权限域
    profile.permission_domain = "department"

    # 标签填 fallback
    profile.content_tags = validate_content_tags({})
    profile.content_tag_confidences = {
        dim: 0.0 for dim in
        ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag")
    }
    profile.tagging_source = "fallback"
    profile.suggested_tags = []

    # 摘要：截取前文
    profile.summary_short = (content[:50] if content else "")
    profile.summary_search = (content[:300] if content else "")
    profile.summary_embedding = (content[:300] if content else "")
    profile.summarization_source = "fallback"
