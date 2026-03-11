"""Auto attribution: match version diff changes to suggestion contributions."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillAttribution, SkillSuggestion, AttributionLevel
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_ATTRIBUTION_SYSTEM = """你是贡献归因分析助手。给定一个Skill的版本变更（diff）以及引发本次迭代的用户意见列表，
分析每条意见对本次变更的贡献程度。

## 版本变更 (v{from_ver} → v{to_ver})
{diff_text}

## 用户意见列表
{suggestions_text}

## 输出要求
只返回 JSON 数组，不要 markdown 代码块。格式：
[
  {{
    "suggestion_id": 1,
    "attribution_level": "full",
    "matched_change": "在prompt中增加了竞品分析模块",
    "reason": null
  }},
  {{
    "suggestion_id": 2,
    "attribution_level": "partial",
    "matched_change": "部分采纳了数据可视化的建议",
    "reason": "原意见要求图表，实际只增加了表格格式"
  }},
  {{
    "suggestion_id": 3,
    "attribution_level": "none",
    "matched_change": null,
    "reason": "该意见关于语气调整，本次未涉及"
  }}
]
attribution_level 取值：full（完全采纳）/ partial（部分采纳）/ none（未采纳）"""


class AttributionService:

    async def generate_attributions(
        self,
        skill_id: int,
        version_from: int,
        version_to: int,
        suggestion_ids: list[int],
        model_config: dict,
        db: Session,
    ) -> list[SkillAttribution]:
        """Call LLM to match changes to suggestions, write results to DB."""
        skill = db.get(Skill, skill_id)
        if not skill:
            return []

        # Get version prompts for diff
        versions = {v.version: v for v in skill.versions}
        v_from = versions.get(version_from)
        v_to = versions.get(version_to)

        if not v_from or not v_to:
            logger.warning(f"Cannot find versions {version_from} or {version_to} for skill {skill_id}")
            return []

        # Build diff text
        diff_parts = []
        if v_from.system_prompt != v_to.system_prompt:
            diff_parts.append(f"System Prompt 发生变更（旧 {len(v_from.system_prompt)} 字 → 新 {len(v_to.system_prompt)} 字）")
            # Show a short excerpt of new content
            new_excerpt = v_to.system_prompt[:500]
            diff_parts.append(f"新Prompt摘要：{new_excerpt}...")
        if (v_from.variables or []) != (v_to.variables or []):
            diff_parts.append(f"变量变更：{v_from.variables} → {v_to.variables}")
        diff_text = "\n".join(diff_parts) if diff_parts else "（无可检测的文本差异）"

        # Get suggestions text
        suggestions = (
            db.query(SkillSuggestion)
            .filter(SkillSuggestion.id.in_(suggestion_ids))
            .all()
        )
        suggestions_text = "\n\n".join(
            f"意见 #{s.id}:\n问题：{s.problem_desc}\n期望：{s.expected_direction}"
            + (f"\n示例：{s.case_example}" if s.case_example else "")
            for s in suggestions
        )

        system = _ATTRIBUTION_SYSTEM.format(
            from_ver=version_from,
            to_ver=version_to,
            diff_text=diff_text,
            suggestions_text=suggestions_text,
        )

        try:
            result, _ = await llm_gateway.chat(
                model_config=model_config,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": "请分析每条意见的贡献程度。"},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            items = json.loads(cleaned)
        except Exception as e:
            logger.error(f"Attribution LLM failed: {e}")
            return []

        created = []
        for item in items:
            level_str = item.get("attribution_level", "none")
            try:
                level = AttributionLevel(level_str)
            except ValueError:
                level = AttributionLevel.NONE

            attr = SkillAttribution(
                skill_id=skill_id,
                version_from=version_from,
                version_to=version_to,
                suggestion_id=item["suggestion_id"],
                attribution_level=level,
                matched_change=item.get("matched_change"),
                reason=item.get("reason"),
            )
            db.add(attr)
            created.append(attr)

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Attribution DB commit failed: {e}")

        return created


attribution_service = AttributionService()
