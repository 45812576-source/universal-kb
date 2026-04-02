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

## 文档内容（前3000字）
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
    "subject_tag": "谁产出/使用（从词表选或新建）",
    "object_tag": "涉及什么对象",
    "scenario_tag": "什么场景下使用",
    "action_tag": "做什么动作",
    "industry_or_domain_tag": "属于什么行业/领域"
  }},
  "suggested_tags": ["自由标签1", "自由标签2"],
  "summary_short": "≤100字的一句话摘要（{summary_instruction}）",
  "summary_search": "≤300字的检索用摘要，包含关键实体和方法论名词（{summary_instruction}）",
  "quality_score": 0.85
}}

## 摘要生成规则:
- 若摘要脱敏模式为 raw: 正常生成
- 若为 masked: 精确金额/手机号等用***替代
- 若为 abstracted: 敏感实体用通用代称（"某客户"、"某金额范围"）

## 质量评分标准（0-1）:
- 0.9+: 有独特方法论/数据洞察/完整案例
- 0.7-0.9: 有价值的经验总结或操作指南
- 0.5-0.7: 一般性信息
- <0.5: 碎片化或无效内容"""


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

        # ── Step 8: 结果落库 ────────────────────────────────────────────
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
    """合并一次 LLM 调用，产出标题/标签/摘要。"""
    summary_instructions = {
        "raw": "正常生成，无需脱敏",
        "masked": "精确金额/手机号等敏感数据用***替代",
        "abstracted": "敏感实体用通用代称，如\"某客户\"\"某金额范围\"",
    }

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
        summary_instruction=summary_instructions.get(summary_mode, "正常生成"),
    )

    model_config = llm_gateway.resolve_config(db, "knowledge.understand")
    result_str, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1500,
    )

    # 清理 markdown 代码块
    result_str = result_str.strip()
    if result_str.startswith("```"):
        result_str = result_str.split("```")[1]
        if result_str.startswith("json"):
            result_str = result_str[4:]
    result_str = result_str.strip()

    return json.loads(result_str)


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
        profile.content_tags = validate_content_tags(raw_tags)
        profile.tagging_source = "llm"

    # 建议标签
    suggested = result.get("suggested_tags", [])
    if isinstance(suggested, list):
        profile.suggested_tags = suggested[:10]

    # 摘要
    profile.summary_short = (result.get("summary_short") or "")[:200]
    profile.summary_search = (result.get("summary_search") or "")[:500]
    profile.summarization_source = "llm"


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
    profile.tagging_source = "fallback"
    profile.suggested_tags = []

    # 摘要：截取前 200 字
    profile.summary_short = (content[:100] if content else "")
    profile.summary_search = (content[:300] if content else "")
    profile.summarization_source = "fallback"
