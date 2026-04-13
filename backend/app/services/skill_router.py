"""SkillRouter — 技能匹配/切换路由模块，从 skill_engine 抽出。"""
from __future__ import annotations

import asyncio
import logging
import re
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillStatus
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_SKILL_MATCH_PROMPT = """你是意图识别系统。根据用户消息从可用Skill中选择最匹配的一个。

规则：
- 只返回Skill的 name（精确匹配列表中的名称），不要解释、不要返回多个
- 若没有合适的Skill，返回字符串 "none"
- 优先匹配用户意图而非关键词

示例：
用户: "帮我写一篇小红书种草文" → content-writing（如果存在内容写作技能）
用户: "今天天气怎么样" → none（闲聊不匹配任何技能）
用户: "把刚才的分析做成PPT" → pptx-generation（明确的工具需求）

可用Skills:
{skill_list}

用户消息: {user_message}"""


class SkillRouter:
    """技能匹配/切换路由。"""

    async def match_or_keep_skill(
        self,
        db: Session,
        current_skill: Skill,
        user_message: str,
        candidates: list[Skill],
    ) -> Skill:
        """一次 LLM 调用完成「是否切换 + 切换到哪个」判断。
        返回应使用的 Skill（可能是 current_skill 本身）。
        """
        if not candidates:
            return current_skill

        skill_list = "\n".join(
            f"- {s.name}: {(s.description or '无描述')[:30]}" for s in candidates
        )
        prompt = (
            f"当前技能：{current_skill.name}（{current_skill.description or '无描述'}）\n"
            f"用户新消息：{user_message}\n\n"
            f"可切换的其他技能：\n{skill_list}\n\n"
            "判断规则：\n"
            "- 只有用户明确说出切换意图（如「换一个」「用XX技能」「切换到」），才返回目标技能的 name\n"
            "- 继续当前话题、追问、补充信息、说「好的」「继续」等，一律返回 keep\n"
            "- 拿不准时，返回 keep\n"
            "只返回一个词。"
        )
        try:
            result, _ = await asyncio.wait_for(
                llm_gateway.chat(
                    model_config=llm_gateway.resolve_config(db, "skill.switch_detect"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=50,
                ),
                timeout=12.0,
            )
            answer = result.strip().splitlines()[0].strip().strip('"').strip("'")
            if answer.lower() == "keep":
                return current_skill
            for s in candidates:
                if s.name == answer:
                    return s
            return current_skill
        except asyncio.TimeoutError:
            logger.warning("Skill match_or_keep timed out after 12s, keeping current")
            return current_skill
        except Exception as e:
            logger.warning(f"Skill match_or_keep failed: {e}, keeping current")
            return current_skill

    async def refresh_skill_routing_prompt(self, db: Session, user_config) -> None:
        """根据已挂载 Skill 列表生成/更新路由 prompt。支持增量更新。"""
        mounted_ids = [
            item["skill_id"]
            for item in (user_config.mounted_skills or [])
            if item.get("mounted")
        ]
        if not mounted_ids:
            user_config.skill_routing_prompt = None
            user_config.last_skill_snapshot = None
            user_config.needs_prompt_refresh = False
            db.flush()
            return

        skills = db.query(Skill).filter(Skill.id.in_(mounted_ids)).all()
        new_snapshot = [{"name": s.name, "description": s.description or ""} for s in skills]

        old_snapshot = user_config.last_skill_snapshot or []
        old_names = {s["name"] for s in old_snapshot}
        new_names = {s["name"] for s in new_snapshot}

        added = [s for s in new_snapshot if s["name"] not in old_names]
        removed = [s for s in old_snapshot if s["name"] not in new_names]

        try:
            if not old_snapshot or not user_config.skill_routing_prompt:
                skill_list = "\n".join(
                    f"- {s['name']}: {s['description']}" for s in new_snapshot
                )
                prompt = (
                    "你是 Skill 路由专家。根据以下 Skill 列表，生成一段简洁的路由指引，"
                    "告知 AI 助手在什么场景下应该使用哪个 Skill。\n\n"
                    f"Skill 列表:\n{skill_list}\n\n"
                    "输出格式:\n"
                    "## Skill 路由指引\n"
                    "当用户请求匹配以下场景时，优先使用对应 Skill：\n"
                    "- [场景描述] → 使用 [skill_name]\n"
                    "...\n\n"
                    "如果用户请求不明确匹配以上任何 Skill，使用通用对话模式。\n\n"
                    "直接输出路由指引内容，不要有其他解释。"
                )
            else:
                added_text = "\n".join(f"- {s['name']}: {s['description']}" for s in added) if added else "无"
                removed_text = "\n".join(f"- {s['name']}" for s in removed) if removed else "无"
                prompt = (
                    "你是 Skill 路由专家。请更新以下路由指引。\n\n"
                    f"当前路由指引:\n{user_config.skill_routing_prompt}\n\n"
                    f"新增 Skill:\n{added_text}\n\n"
                    f"移除 Skill:\n{removed_text}\n\n"
                    "请输出更新后的完整路由指引，保持原有格式。"
                    "直接输出内容，不要有其他解释。"
                )

            config = llm_gateway.resolve_config(db, "skill.routing_prompt")
            result, _ = await llm_gateway.chat(
                config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            routing_prompt = result.strip() if isinstance(result, str) else str(result).strip()

            if routing_prompt:
                user_config.skill_routing_prompt = routing_prompt
        except Exception as e:
            logger.warning(f"Skill routing prompt generation failed: {e}")
            lines = [
                "## Skill 路由指引\n",
                "当用户请求匹配以下场景时，优先使用对应 Skill：",
            ]
            for s in new_snapshot:
                lines.append(f"- {s['description'] or s['name']} → 使用 {s['name']}")
            lines.append("\n如果用户请求不明确匹配以上任何 Skill，使用通用对话模式。")
            user_config.skill_routing_prompt = "\n".join(lines)

        user_config.last_skill_snapshot = new_snapshot
        user_config.needs_prompt_refresh = False
        db.flush()

    async def match_skill(
        self, db: Session, user_message: str, model_config: dict,
        candidate_skills: list[Skill] | None = None,
        allow_global_fallback: bool = True,
    ) -> Skill | None:
        """匹配最佳 Skill。

        Args:
            allow_global_fallback: 是否允许回退到全局 published skills（workspace 边界控制）。
                当 candidate_skills 已提供且 allow_global_fallback=False 时，
                仅在候选集内匹配，不查全局。
        """
        if candidate_skills is None:
            if not allow_global_fallback:
                return None
            skills = (
                db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()
            )
        else:
            skills = candidate_skills

        if not skills:
            return None

        if len(skills) == 1:
            return skills[0]

        # 超过 15 个候选时，先用关键词粗筛
        if len(skills) > 15:
            msg_lower = user_message.lower()
            msg_words = [w for w in msg_lower.split() if len(w) > 1]
            def _kw_score(s: Skill) -> int:
                text = f"{s.name} {s.description or ''}".lower()
                return sum(1 for w in msg_words if w in text)
            scored = sorted(((s, _kw_score(s)) for s in skills), key=lambda x: x[1], reverse=True)
            top = [s for s, sc in scored[:15] if sc > 0]
            skills = top if top else [s for s, _ in scored[:15]]

        # 注入执行统计信号
        import datetime as _dt
        from sqlalchemy import func as _sa_func
        from app.models.skill import SkillExecutionLog
        _since = _dt.datetime.utcnow() - _dt.timedelta(days=30)
        from sqlalchemy import Integer as _SAInt
        _stats_q = (
            db.query(
                SkillExecutionLog.skill_id,
                _sa_func.count(SkillExecutionLog.id).label("cnt"),
                _sa_func.avg(
                    _sa_func.cast(SkillExecutionLog.success, _SAInt)
                ).label("sr"),
            )
            .filter(SkillExecutionLog.created_at >= _since)
            .group_by(SkillExecutionLog.skill_id)
            .all()
        )
        _skill_stats = {r.skill_id: (r.cnt, round(float(r.sr or 0) * 100)) for r in _stats_q}

        def _fmt(s: Skill) -> str:
            base = f"- {s.name}: {(s.description or '无描述')[:30]}"
            st = _skill_stats.get(s.id)
            if st:
                base += f" [可靠性:{st[1]}%, 使用量:{st[0]}]"
            return base
        skill_list = "\n".join(_fmt(s) for s in skills)
        prompt = _SKILL_MATCH_PROMPT.format(
            skill_list=skill_list, user_message=user_message
        )
        import time as _time
        try:
            match_config = llm_gateway.resolve_config(db, "skill.match")
        except Exception:
            match_config = model_config
        _t0 = _time.monotonic()
        logger.debug(f"[match_skill] calling {match_config.get('model_id')} with {len(skills)} skills")
        try:
            result, _ = await asyncio.wait_for(
                llm_gateway.chat(
                    model_config=match_config,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=50,
                ),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[match_skill] LLM timed out after 12s, returning None")
            return None
        logger.debug(f"[match_skill] LLM done in {_time.monotonic()-_t0:.2f}s, raw='{result[:80]}'")

        first_line = result.strip().splitlines()[0].strip().strip('"').strip("'")
        if first_line.lower() == "none":
            return None
        for s in skills:
            if s.name == first_line:
                return s
        result_lower = result.lower()
        for s in skills:
            if s.name.lower() in result_lower:
                return s
        return None

    def resolve_candidates(
        self,
        db: Session,
        *,
        workspace,
        workspace_skills: list[Skill],
        user_id: int | None,
        active_skill_ids: list[int] | None,
        current_skill: Skill | None,
        allow_global_fallback: bool = True,
    ) -> tuple[Skill | None, list[Skill], bool]:
        """解析候选 Skill 列表和匹配策略。

        Returns:
            (current_skill_if_locked, candidates, need_match)
            - 如果 current_skill 仍有效，返回 (current_skill, switch_candidates, True)
            - 否则返回 (None, merged_candidates, True)
        """
        # active_skill_ids 限制
        if current_skill and active_skill_ids is not None and current_skill.id not in active_skill_ids:
            current_skill = None

        if current_skill:
            switch_candidates = [s for s in (workspace_skills or []) if s.id != current_skill.id]
            return current_skill, switch_candidates, bool(switch_candidates)

        # 无 current_skill：构建候选列表
        if workspace and workspace_skills:
            global_skills = (
                db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()
            ) if allow_global_fallback else []
            seen_ids = {s.id for s in workspace_skills}
            merged = list(workspace_skills) + [s for s in global_skills if s.id not in seen_ids]
            if active_skill_ids is not None:
                merged = [s for s in merged if s.id in active_skill_ids]
            return None, merged, True

        # 无 workspace：加载个人工作台配置
        _personal_skills = []
        if user_id:
            try:
                from app.models.workspace import UserWorkspaceConfig
                _user_config = (
                    db.query(UserWorkspaceConfig)
                    .filter(UserWorkspaceConfig.user_id == user_id)
                    .first()
                )
                if _user_config and _user_config.mounted_skills:
                    mounted_ids = [
                        item["skill_id"]
                        for item in _user_config.mounted_skills
                        if item.get("mounted")
                    ]
                    if mounted_ids:
                        _personal_skills = [
                            s for s in db.query(Skill).filter(Skill.id.in_(mounted_ids)).all()
                            if s is not None
                        ]
            except Exception as e:
                logger.warning(f"UserWorkspaceConfig load failed: {e}")

        if active_skill_ids is not None:
            if _personal_skills:
                candidates = [s for s in _personal_skills if s.id in active_skill_ids]
            else:
                candidates = [
                    db.get(Skill, sid) for sid in active_skill_ids
                    if db.get(Skill, sid) is not None
                ]
            return None, candidates, True

        if _personal_skills:
            return None, _personal_skills, True

        # 完全无候选：是否回退到全局
        if allow_global_fallback:
            return None, [], True  # match_skill 会在内部查全局
        return None, [], False


skill_router = SkillRouter()
