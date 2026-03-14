"""Skill Editor: AI-powered natural language skill editing with diff preview."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillSuggestion, SkillVersion
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_EDIT_SYSTEM = """你是 Skill 编辑助手。根据用户的修改指令，对 Skill 进行精准修改。

## 当前 Skill 结构
{skill_json}

## 修改指令
{instruction}

## 要求
- 理解用户意图，对 Skill 的 system_prompt、variables、required_inputs、knowledge_tags、data_queries 进行修改
- 只修改需要改动的字段，其他保持不变
- 输出严格 JSON，不要 markdown 代码块，格式：
{{
  "system_prompt": "完整的新 prompt 内容",
  "variables": ["var1", "var2"],
  "required_inputs": [{{"key": "product", "label": "产品名称", "desc": "你的具体产品是什么", "example": "XX猫粮"}}],
  "knowledge_tags": ["tag1", "tag2"],
  "data_queries": [...],
  "change_note": "本次修改说明"
}}
- required_inputs 是用户开始任务前必须提供的信息项，每项含 key/label/desc/example 四个字段
- 如果某字段无需修改，保留原值
- change_note 用中文简短描述本次修改"""


class SkillEditor:

    async def edit_skill(
        self,
        skill_id: int,
        instruction: str,
        model_config: dict,
        db: Session,
    ) -> dict:
        """
        Use LLM to generate a diff preview based on natural language instruction.
        Returns a preview dict with old/new values, not yet saved.
        """
        skill = db.get(Skill, skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")

        latest = skill.versions[0] if skill.versions else None

        current = {
            "name": skill.name,
            "description": skill.description or "",
            "mode": skill.mode.value if skill.mode else "hybrid",
            "knowledge_tags": skill.knowledge_tags or [],
            "auto_inject": skill.auto_inject,
            "data_queries": skill.data_queries or [],
            "system_prompt": latest.system_prompt if latest else "",
            "variables": latest.variables or [] if latest else [],
            "required_inputs": latest.required_inputs or [] if latest else [],
            "current_version": latest.version if latest else 0,
        }

        system = _EDIT_SYSTEM.format(
            skill_json=json.dumps(current, ensure_ascii=False, indent=2),
            instruction=instruction,
        )

        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": instruction},
            ],
            temperature=0.2,
            max_tokens=4000,
        )

        # Strip markdown code blocks if present
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        proposed = json.loads(cleaned)

        # Build diff: only fields that changed
        diff = {}
        for field in ("system_prompt", "variables", "required_inputs", "knowledge_tags", "data_queries"):
            old_val = current.get(field)
            new_val = proposed.get(field)
            if old_val != new_val:
                diff[field] = {"old": old_val, "new": new_val}

        return {
            "skill_id": skill_id,
            "skill_name": skill.name,
            "current_version": current["current_version"],
            "proposed": proposed,
            "diff": diff,
            "change_note": proposed.get("change_note", instruction[:100]),
        }

    def apply_edit(
        self,
        skill_id: int,
        proposed: dict,
        change_note: str,
        user_id: int,
        db: Session,
    ) -> dict:
        """
        Apply the proposed edit: update skill metadata and create a new version.
        """
        skill = db.get(Skill, skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")

        # Update skill-level fields if present
        if "knowledge_tags" in proposed:
            skill.knowledge_tags = proposed["knowledge_tags"]
        if "data_queries" in proposed:
            skill.data_queries = proposed["data_queries"]

        # Create new version
        latest = skill.versions[0] if skill.versions else None
        max_ver = max((v.version for v in skill.versions), default=0)

        new_version = SkillVersion(
            skill_id=skill_id,
            version=max_ver + 1,
            system_prompt=proposed.get(
                "system_prompt",
                latest.system_prompt if latest else "",
            ),
            variables=proposed.get(
                "variables",
                latest.variables if latest else [],
            ),
            required_inputs=proposed.get(
                "required_inputs",
                latest.required_inputs if latest else [],
            ),
            output_schema=proposed.get(
                "output_schema",
                latest.output_schema if latest else None,
            ),
            model_config_id=latest.model_config_id if latest else None,
            created_by=user_id,
            change_note=change_note,
        )
        db.add(new_version)
        db.commit()
        db.refresh(new_version)

        return {"version": new_version.version, "id": new_version.id}


    async def iterate_from_suggestions(
        self,
        skill_id: int,
        suggestion_ids: list[int],
        model_config: dict,
        db: Session,
    ) -> dict:
        """
        Generate a new version diff based on adopted suggestions.
        Returns same format as edit_skill() — diff preview, not yet saved.
        """
        skill = db.get(Skill, skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")

        suggestions = (
            db.query(SkillSuggestion)
            .filter(SkillSuggestion.id.in_(suggestion_ids))
            .all()
        )
        if not suggestions:
            raise ValueError("No suggestions found for given IDs")

        latest = skill.versions[0] if skill.versions else None
        current = {
            "name": skill.name,
            "description": skill.description or "",
            "mode": skill.mode.value if skill.mode else "hybrid",
            "knowledge_tags": skill.knowledge_tags or [],
            "auto_inject": skill.auto_inject,
            "data_queries": skill.data_queries or [],
            "system_prompt": latest.system_prompt if latest else "",
            "variables": latest.variables or [] if latest else [],
            "required_inputs": latest.required_inputs or [] if latest else [],
            "current_version": latest.version if latest else 0,
        }

        parts = []
        for s in suggestions:
            # 部分采纳：用 review_note（管理者框选的片段）替代完整内容
            if s.status.value == "partial" and s.review_note:
                parts.append(f"意见 #{s.id}（部分采纳）:\n{s.review_note}")
            else:
                entry = f"意见 #{s.id}:\n问题：{s.problem_desc}\n期望方向：{s.expected_direction}"
                if s.case_example:
                    entry += f"\n示例：{s.case_example}"
                parts.append(entry)
        suggestion_text = "\n\n".join(parts)

        system = _EDIT_SYSTEM.format(
            skill_json=json.dumps(current, ensure_ascii=False, indent=2),
            instruction=f"根据以下用户反馈意见迭代该Skill：\n\n{suggestion_text}",
        )

        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"请根据以下意见生成改进版本：\n\n{suggestion_text}"},
            ],
            temperature=0.2,
            max_tokens=4000,
        )

        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        proposed = json.loads(cleaned)

        diff = {}
        for field in ("system_prompt", "variables", "knowledge_tags", "data_queries"):
            old_val = current.get(field)
            new_val = proposed.get(field)
            if old_val != new_val:
                diff[field] = {"old": old_val, "new": new_val}

        return {
            "skill_id": skill_id,
            "skill_name": skill.name,
            "current_version": current["current_version"],
            "proposed": proposed,
            "diff": diff,
            "change_note": proposed.get("change_note", f"基于{len(suggestions)}条用户意见迭代"),
            "suggestion_ids": [s.id for s in suggestions],
        }


skill_editor = SkillEditor()
