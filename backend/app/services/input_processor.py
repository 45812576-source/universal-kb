"""统一输入处理流水线: normalize → detect&extract → save extraction → build draft"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.draft import Draft, DraftStatus
from app.models.raw_input import (
    DetectedObjectType, InputExtraction,
    RawInput, RawInputStatus,
)
from app.services.llm_gateway import llm_gateway
from app.utils.file_parser import extract_text

logger = logging.getLogger(__name__)

# ── Prompt ─────────────────────────────────────────────────────────────────────

DETECT_AND_EXTRACT_PROMPT = """你是企业知识管理系统的 AI 助手，帮助员工将工作素材自动结构化。

用户将输入原始工作素材（聊天记录、会议纪要、客户沟通、经验总结、客户反馈等）。

你需要完成以下任务：

## 1. 识别对象类型
判断这段内容最适合沉淀为哪种业务对象：
- knowledge：经验总结、方法论、案例、SOP、FAQ、模板、外部资料
- opportunity：销售商机、客户需求、业务拓展线索
- feedback：客户反馈、问题报告、投诉、需求建议
- unknown：无法判断

## 2. 抽取结构化字段

### knowledge 类型字段：
- title: 标题（20字以内）
- content_summary: 内容摘要（100-200字）
- knowledge_type: experience / methodology / case_study / data / template / external
- industry_tags: 行业标签数组，如 ["食品", "美妆"]
- platform_tags: 平台标签数组，如 ["抖音", "小红书"]
- topic_tags: 主题标签数组，如 ["ROI优化", "投放策略"]
- visibility: all（全员）/ department（仅本部门）

### opportunity 类型字段：
- title: 商机标题（20字以内）
- customer_name: 客户名称
- industry: 行业
- stage: lead / contact / needs / proposal / negotiation
- needs_summary: 核心需求摘要（100字以内）
- decision_map: 决策角色列表，每项含 {{name, role, is_decision_maker}}
- risk_points: 风险点数组
- next_actions: 下一步建议数组
- priority: high / normal / low

### feedback 类型字段：
- title: 反馈标题（20字以内）
- customer_name: 客户名称
- feedback_type: bug / feature_request / config_issue / training_issue / churn_risk
- severity: critical / high / medium / low
- description: 问题描述（100字以内）
- affected_module: 影响模块
- renewal_risk_level: high / medium / low
- routed_team: 建议流转团队（如"产品组"/"技术组"/"客成组"）
- knowledgeworthy: true / false（是否值得沉淀为FAQ）

## 3. 评估置信度
对每个字段给出 0.0-1.0 的置信度。

## 4. 生成待确认问题
对置信度 < 0.7 的**关键字段**生成确认问题（最多3个），要求：
- 只问对后续动作最关键的字段
- 必须提供可点选选项
- 问题简洁，3-5秒可答完

## 5. 生成一句话摘要（30字以内）

## 6. 建议后续动作（2-4个）

---

用户原始输入:
{raw_text}

严格返回以下 JSON，不要返回其他任何内容:
{{
  "object_type": "knowledge|opportunity|feedback|unknown",
  "intent": "意图描述",
  "summary": "一句话摘要（30字以内）",
  "fields": {{ ... }},
  "confidence": {{ "field_name": 0.9, ... }},
  "pending_questions": [
    {{
      "field": "field_name",
      "question": "问题",
      "options": ["选项1", "选项2"],
      "type": "single_choice"
    }}
  ],
  "suggested_actions": ["动作1", "动作2"]
}}"""


def _normalize_text(raw_input: RawInput) -> str:
    """将多模态输入标准化为纯文本。MVP 阶段只支持 text + file。"""
    parts = []

    if raw_input.raw_text:
        parts.append(raw_input.raw_text)

    for url in (raw_input.attachment_urls or []):
        try:
            ext = Path(url).suffix.lower()
            if ext in ('.txt', '.pdf', '.docx', '.pptx', '.md'):
                text = extract_text(url)
                parts.append(f"[文件内容: {Path(url).name}]\n{text[:3000]}")
            # 图片/语音/URL 留给 Phase 2 扩展
        except Exception as e:
            logger.warning(f"Failed to extract file {url}: {e}")

    return "\n\n---\n\n".join(parts) if parts else ""


def _parse_object_type(raw: str) -> DetectedObjectType:
    mapping = {
        "knowledge": DetectedObjectType.KNOWLEDGE,
        "opportunity": DetectedObjectType.OPPORTUNITY,
        "feedback": DetectedObjectType.FEEDBACK,
    }
    return mapping.get(raw.lower(), DetectedObjectType.UNKNOWN)


async def process_raw_input(raw_input_id: int, db: Session) -> Draft:
    """主入口：处理一个 raw_input，返回生成的 Draft。"""
    raw_input = db.get(RawInput, raw_input_id)
    if not raw_input:
        raise ValueError(f"RawInput {raw_input_id} not found")

    raw_input.status = RawInputStatus.PROCESSING
    db.flush()

    # Step 1: normalize
    normalized = _normalize_text(raw_input)
    if raw_input.raw_text:
        raw_input.raw_text = normalized

    if not normalized.strip():
        raw_input.status = RawInputStatus.FAILED
        db.commit()
        raise ValueError("Empty content after normalization")

    # Step 2: LLM detect & extract
    try:
        model_config = llm_gateway.get_config(db)
        result_str = await llm_gateway.chat(
            model_config=model_config,
            messages=[{
                "role": "user",
                "content": DETECT_AND_EXTRACT_PROMPT.format(raw_text=normalized[:4000]),
            }],
            temperature=0.1,
            max_tokens=2000,
        )
        # 清理可能的 markdown 代码块
        result_str = result_str.strip()
        if result_str.startswith("```"):
            result_str = result_str.split("```")[1]
            if result_str.startswith("json"):
                result_str = result_str[4:]
        parsed = json.loads(result_str.strip())
    except Exception as e:
        logger.error(f"LLM extraction failed for raw_input {raw_input_id}: {e}")
        raw_input.status = RawInputStatus.FAILED
        db.commit()
        raise

    # Step 3: save extraction
    object_type = _parse_object_type(parsed.get("object_type", "unknown"))
    extraction = InputExtraction(
        raw_input_id=raw_input.id,
        detected_intent=parsed.get("intent", ""),
        detected_object_type=object_type,
        summary=parsed.get("summary", ""),
        fields_json=parsed.get("fields", {}),
        confidence_json=parsed.get("confidence", {}),
        uncertain_fields=[q["field"] for q in parsed.get("pending_questions", [])],
    )
    db.add(extraction)
    db.flush()

    # Step 4: build draft
    fields = parsed.get("fields", {})
    title = (
        fields.get("title")
        or parsed.get("summary", "")[:60]
        or "未命名草稿"
    )
    tags_json = {
        "industry": fields.get("industry_tags", []),
        "platform": fields.get("platform_tags", []),
        "topic": fields.get("topic_tags", []),
    }

    draft = Draft(
        object_type=object_type,
        source_raw_input_id=raw_input.id,
        source_extraction_id=extraction.id,
        conversation_id=raw_input.conversation_id,
        created_by_id=raw_input.created_by_id,
        title=title,
        summary=parsed.get("summary", ""),
        fields_json=fields,
        tags_json=tags_json,
        pending_questions=parsed.get("pending_questions", []),
        suggested_actions=parsed.get("suggested_actions", []),
        status=DraftStatus.WAITING_CONFIRMATION,
    )
    db.add(draft)

    raw_input.status = RawInputStatus.EXTRACTED
    db.commit()
    db.refresh(draft)
    return draft
