"""Skill dispatch engine: intent matching → variable extraction → knowledge injection → LLM call."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.models.conversation import Conversation, Message, MessageRole
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.skill import Skill, SkillMode, SkillStatus
from app.models.user import User
from app.services.llm_gateway import llm_gateway
from app.services import prompt_compiler


def _check_model_grant(db: Session, model_config: dict, user_id: int | None) -> None:
    """如果该 model_id 在 user_model_grants 表中有授权记录，说明是受限模型，
    检查当前用户是否在授权名单中。model_key 格式支持 'model_id' 或 'provider/model_id'。"""
    from app.models.opencode import UserModelGrant
    model_id = model_config.get("model_id", "")
    # 查这个模型是否存在任何授权记录（即是否是受限模型）
    any_grant = (
        db.query(UserModelGrant)
        .filter(
            (UserModelGrant.model_key == model_id) |
            UserModelGrant.model_key.like(f"%/{model_id}")
        )
        .first()
    )
    if any_grant is None:
        return  # 不是受限模型，放行
    if user_id is None:
        raise HTTPException(403, f"模型 {model_id} 需要授权才能使用")
    user_grant = (
        db.query(UserModelGrant)
        .filter(
            UserModelGrant.user_id == user_id,
            (UserModelGrant.model_key == model_id) |
            UserModelGrant.model_key.like(f"%/{model_id}")
        )
        .first()
    )
    if user_grant is None:
        raise HTTPException(403, f"您没有使用模型 {model_id} 的权限，请联系管理员申请授权")


# ── 附属文件运行时注入 ────────────────────────────────────────────────────────

_FILE_CATEGORY_HEADERS = {
    "knowledge-base": "知识库",
    "example": "示例",
    "reference": "参考资料",
    "template": "输出模板",
}

_TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".sh", ".toml", ".xml", ".csv", ".html", ".css", ".sql",
}


def _read_source_files(
    skill_id: int,
    source_files: list[dict],
    max_total_chars: int = 30000,
) -> str:
    """读取 skill 附属文件，按 category 分组注入。跳过二进制、过大文件。"""
    from pathlib import Path

    grouped: dict[str, list[tuple[str, str]]] = {}
    total = 0
    for f in source_files:
        fname = f.get("filename", "")
        category = f.get("category", "other")
        if category not in _FILE_CATEGORY_HEADERS:
            continue
        ext = fname[fname.rfind("."):].lower() if "." in fname else ""
        if ext not in _TEXT_EXTENSIONS:
            continue
        fpath = Path(f"uploads/skills/{skill_id}") / Path(fname).name
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
        except Exception:
            continue
        if total + len(content) > max_total_chars:
            break
        total += len(content)
        grouped.setdefault(category, []).append((fname, content))

    if not grouped:
        return ""

    parts = []
    for cat in ["knowledge-base", "example", "reference", "template"]:
        files = grouped.get(cat)
        if not files:
            continue
        header = _FILE_CATEGORY_HEADERS[cat]
        for fname, content in files:
            parts.append(f"\n\n## {header}：{fname}\n\n{content}")
    return "".join(parts)


@dataclass
class PrepareResult:
    """Output of skill_engine.prepare() — everything needed to call LLM."""
    llm_messages: list[dict]
    model_config: dict
    skill_name: str | None = None
    skill_id: int | None = None
    skill_version: Any = None
    workspace: Any = None
    available_tools: list = field(default_factory=list)
    # OpenAI-compatible tools schema，当模型支持 function calling 时传入 LLM
    tools_schema: list[dict] = field(default_factory=list)
    # 非流式短路结果：如果 prepare 阶段已经产出了最终回复（rule engine / data query / input eval），
    # 则 early_return 非 None，调用方应直接返回该结果而不调用 LLM。
    early_return: tuple[str, dict] | None = None

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

_PARAM_EXTRACT_PROMPT = """从对话中提取以下变量的值。

规则：
- 若某变量无法从对话中确定，值设为 null，不要猜测
- 可以从上下文推断（如用户说"帮我分析这个品牌"，前文提到了品牌名，则提取该品牌名）
- 只返回 JSON 对象，不要包含其他内容

示例输出：{{"product": "XX冻干猫粮", "channel": null, "target": "养猫女性"}}

需要提取的变量: {variables}
对话内容:
{conversation}"""

_DEFAULT_SYSTEM = (
    "你是 Le Desk 企业知识助手，服务于企业内部团队。\n\n"
    "## 回复规范\n"
    "- 直接回答问题，不要加不必要的前缀（如「好的」「根据你的问题」「我来帮你」）或后缀（如「希望对你有帮助」）\n"
    "- 回复简洁精准，优先用结构化格式（列表、表格）呈现复杂信息\n"
    "- 如果引用了参考知识，在回答末尾用「📎 参考知识」标注来源\n"
    "- 如果知识不足以回答，如实说明并建议获取信息的途径，不要编造\n\n"
    "## 禁止行为\n"
    "- 不要重复用户的问题\n"
    "- 不要在回答中展示原始 JSON 数据\n"
    "- 不要自我介绍或解释你的能力\n"
    "- 不要在回复末尾加任何引导语，如「不吝点赞」「欢迎转发」「觉得有用请收藏」「关注我」等\n\n"
    "## 工具缺失时的处理\n"
    "- 当用户提出需要特定工具才能完成的请求（如创建日程、发邮件、查数据库等），但当前没有对应工具时：\n"
    "  1. 明确告知用户该功能暂不支持\n"
    "  2. 如果有 task_creator 工具可用，主动询问是否改为创建一个待办任务来跟踪此事\n"
    "  3. 如果连 task_creator 也没有，简洁说明并建议用户手动处理\n"
    "- 不要假装能做到、也不要沉默跳过，要给用户一个明确的替代方案\n"
    "如果引用了知识，请在回答末尾标注「参考知识」。"
    "如果知识不足以回答，请如实说明。"
)


class SkillEngine:

    @staticmethod
    def _build_tools_schema(tools: list) -> list[dict]:
        """将 ToolRegistry 列表转换为 OpenAI function calling schema 格式。"""
        result = []
        for t in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or t.display_name or t.name)[:1024],
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })
        return result

    async def _match_or_keep_skill(
        self,
        db: Session,
        current_skill: "Skill",
        user_message: str,
        candidates: list["Skill"],
    ) -> "Skill":
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
            logger.warning(f"Skill match_or_keep timed out after 12s, keeping current")
            return current_skill
        except Exception as e:
            logger.warning(f"Skill match_or_keep failed: {e}, keeping current")
            return current_skill

    async def _refresh_skill_routing_prompt(self, db: Session, user_config) -> None:
        """根据已挂载 Skill 列表生成/更新路由 prompt。支持增量更新。"""
        from app.models.skill import Skill

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
                # 首次：全量生成
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
                # 增量更新
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

            from app.services.llm_gateway import llm_gateway as _gw
            config = _gw.resolve_config(db, "skill.routing_prompt")
            result, _ = await _gw.chat(
                config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            routing_prompt = result.strip() if isinstance(result, str) else str(result).strip()

            if routing_prompt:
                user_config.skill_routing_prompt = routing_prompt
        except Exception as e:
            logger.warning(f"Skill routing prompt generation failed: {e}")
            # 生成失败时使用简单模板
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

    async def _match_skill(
        self, db: Session, user_message: str, model_config: dict,
        candidate_skills: list[Skill] | None = None,
    ) -> Skill | None:
        if candidate_skills is None:
            skills = (
                db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()
            )
        else:
            skills = candidate_skills

        if not skills:
            return None

        # 只有一个候选时直接返回，无需 LLM
        if len(skills) == 1:
            return skills[0]

        # 超过 15 个候选时，先用关键词粗筛，把候选压缩到 ≤15 个，减少 prompt 长度
        if len(skills) > 15:
            msg_lower = user_message.lower()
            msg_words = [w for w in msg_lower.split() if len(w) > 1]
            def _kw_score(s: Skill) -> int:
                text = f"{s.name} {s.description or ''}".lower()
                return sum(1 for w in msg_words if w in text)
            scored = sorted(((s, _kw_score(s)) for s in skills), key=lambda x: x[1], reverse=True)
            top = [s for s, sc in scored[:15] if sc > 0]
            skills = top if top else [s for s, _ in scored[:15]]

        # description 截断到 30 字，避免 prompt 过长导致模型不遵循指令
        skill_list = "\n".join(
            f"- {s.name}: {(s.description or '无描述')[:30]}" for s in skills
        )
        prompt = _SKILL_MATCH_PROMPT.format(
            skill_list=skill_list, user_message=user_message
        )
        import time as _time
        try:
            match_config = llm_gateway.resolve_config(db, "skill.match")
        except Exception:
            match_config = model_config
        _t0 = _time.monotonic()
        logger.debug(f"[_match_skill] calling {match_config.get('model_id')} with {len(skills)} skills, prompt len={len(prompt)}")
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
            logger.warning(f"[_match_skill] LLM timed out after 12s, returning None")
            return None
        logger.debug(f"[_match_skill] LLM done in {_time.monotonic()-_t0:.2f}s, raw='{result[:80]}'")

        # 先取第一行，再在全文中扫描 skill name（应对模型返回解释性文字的情况）
        first_line = result.strip().splitlines()[0].strip().strip('"').strip("'")
        if first_line.lower() == "none":
            return None
        for s in skills:
            if s.name == first_line:
                return s
        # fallback: 扫描全文找第一个匹配的 skill name
        result_lower = result.lower()
        for s in skills:
            if s.name.lower() in result_lower:
                return s
        return db.query(Skill).filter(Skill.name == first_line).first()

    async def _extract_variables(
        self,
        variables: list[str],
        conversation_text: str,
        model_config: dict,
    ) -> dict:
        if not variables:
            return {}
        prompt = _PARAM_EXTRACT_PROMPT.format(
            variables=", ".join(variables),
            conversation=conversation_text,
        )
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        try:
            return json.loads(result.strip())
        except json.JSONDecodeError:
            return {}

    async def _rerank_hits_with_llm(
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

    async def _inject_knowledge(
        self,
        query: str,
        skill: Skill | None,
        db=None,
        user_id: int | None = None,
        project_id: int | None = None,
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
            # 构建检索参数：如果有匹配的 Skill，用其 serving_skill_codes 对应的 taxonomy_board 做预过滤
            search_kwargs = {"query": query, "top_k": 20}

            # Skill 关联的 taxonomy_board 预过滤（减少跨领域噪音）
            if skill and hasattr(skill, "taxonomy_board") and skill.taxonomy_board:
                search_kwargs["taxonomy_board"] = skill.taxonomy_board

            # 过滤低质量知识
            search_kwargs["min_quality"] = 0.3

            # search_knowledge 是同步阻塞调用（pymilvus），Milvus 不可用时会阻塞 ~10s
            # 用 asyncio.wait_for + to_thread 包裹，超时立即返回空，不阻塞事件循环
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

        # 条件精排：高置信度或候选数少时跳过 LLM 精排
        if len(hits) <= 5:
            pass  # 无需精排
        elif len(hits) >= 5 and all(h.get("score", 0) > 0.75 for h in hits[:5]):
            hits = hits[:5]  # top5 高置信度，直接截断
        else:
            hits = await self._rerank_hits_with_llm(db, query, hits, top_k=5)

        # 查询哪些 knowledge_id 是已审批的（全局可见）或项目关联的（项目成员全量可见）
        approved_ids: set[int] = set()
        if db:
            try:
                from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
                approved_entries = (
                    db.query(KnowledgeEntry.id)
                    .filter(KnowledgeEntry.status == KnowledgeStatus.APPROVED)
                    .all()
                )
                approved_ids = {row[0] for row in approved_entries}
            except Exception as e:
                logger.warning(f"Failed to load approved knowledge ids: {e}")

            # 项目知识：对项目成员全量可见（原文注入，不脱敏）
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

        # 批量预查文档脱敏级别（从 KnowledgeUnderstandingProfile）
        doc_levels: dict[int, str] = {}
        doc_data_type_hits: dict[int, list[dict]] = {}
        if db:
            try:
                from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
                kid_set = {h["knowledge_id"] for h in hits}
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

        parts = []
        seen_ids: set[int] = set()
        for h in hits:
            kid = h["knowledge_id"]
            if kid in seen_ids:
                continue
            seen_ids.add(kid)

            chunk_owner = h.get("created_by", 0)
            is_own = user_id and chunk_owner == user_id
            is_approved = kid in approved_ids
            doc_level = doc_levels.get(kid, "D1")

            if is_own or doc_level == "D0":
                # 自己创建的 or 公开文档 → 原文注入
                parts.append(f"[相关知识]\n{h['text']}")
            elif is_approved and doc_level <= "D1":
                # 已审批 + 低敏感 → 原文注入
                parts.append(f"[相关知识]\n{h['text']}")
            else:
                # 需要脱敏：按级别动态执行
                try:
                    from app.services.text_masker import mask_text
                    type_hits = doc_data_type_hits.get(kid)
                    masked_text, _ = mask_text(h["text"], level=doc_level, data_type_hits=type_hits)
                except Exception:
                    # fallback 到预存脱敏版
                    masked_text = h.get("desensitized_text", "").strip()
                    if not masked_text:
                        from app.services.vector_service import _desensitize_rule
                        masked_text = _desensitize_rule(h["text"])
                if masked_text:
                    parts.append(f"[参考知识（已脱敏）]\n{masked_text}")

        return "\n\n---\n\n".join(parts)

    async def _handle_data_operation(
        self,
        db: Session,
        skill: Skill,
        user_message: str,
        model_config: dict,
        user_id: int | None,
        intent_type: str,
    ) -> str:
        """Handle data query or mutation via Text-to-SQL."""
        from app.services.data_engine import data_engine
        from app.models.business import BusinessTable

        # Collect allowed tables from skill's data_queries
        data_queries = skill.data_queries or []
        allowed_table_names = list({q.get("table_name") for q in data_queries if q.get("table_name")})

        # Get table metadata
        tables = data_engine.describe_tables(db)
        # Filter to only tables declared in the skill
        if allowed_table_names:
            tables = [t for t in tables if t["table_name"] in allowed_table_names]

        if not tables:
            return "该 Skill 未关联任何业务数据表，无法执行数据操作。"

        try:
            sql_result = await data_engine.generate_sql(user_message, tables, model_config)
        except Exception as e:
            logger.error(f"SQL generation failed: {e}")
            return f"SQL 生成失败：{e}"

        sql = sql_result.get("sql", "")
        operation = sql_result.get("operation", "read")
        explanation = sql_result.get("explanation", "")

        # Safety validation
        ok, reason = data_engine.validate_sql(sql, operation, allowed_table_names)
        if not ok:
            return f"操作被拒绝：{reason}"

        # Execute
        exec_result = await data_engine.execute_sql(
            db=db,
            sql=sql,
            operation=operation,
            user_id=user_id,
            table_name=allowed_table_names[0] if allowed_table_names else "",
        )

        if not exec_result["ok"]:
            return f"执行失败：{exec_result.get('error', '未知错误')}"

        if operation == "read":
            rows = exec_result["rows"]
            columns = exec_result["columns"]

            # ── 权限：对查询结果做字段级脱敏 ──
            if rows and user_id and skill:
                try:
                    from app.services.data_visibility import data_visibility
                    caller = db.get(User, user_id)
                    if caller:
                        # 查找 table 对应的 data_domain_id
                        _table_name = allowed_table_names[0] if allowed_table_names else ""
                        _bt = db.query(BusinessTable).filter(
                            BusinessTable.table_name == _table_name
                        ).first() if _table_name else None
                        _ownership = _bt.ownership if _bt and hasattr(_bt, 'ownership') else None
                        # 获取 data_domain_id（从 DataScopePolicy 或 BusinessTable）
                        _domain_id = None
                        try:
                            from app.models.permission import DataScopePolicy
                            _dsp = db.query(DataScopePolicy).filter(
                                DataScopePolicy.resource_type == "business_table",
                                DataScopePolicy.resource_id == (_bt.id if _bt else 0),
                            ).first()
                            _domain_id = _dsp.data_domain_id if _dsp else None
                        except Exception:
                            pass
                        rows = data_visibility.apply_with_permission_engine(
                            rows=rows,
                            user=caller,
                            skill_id=skill.id,
                            data_domain_id=_domain_id,
                            db=db,
                            ownership=_ownership,
                        )
                except Exception as e:
                    logger.warning(f"Data query mask failed: {e}")

            table_str = data_engine.format_results(rows, columns)
            return f"{explanation}\n\n{table_str}" if explanation else table_str
        else:
            affected = exec_result.get("affected_rows", 0)
            return f"操作成功，影响 {affected} 行。\n\n{explanation}"

    async def prepare(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        user_id: int | None = None,
        active_skill_ids: list[int] | None = None,
        force_skill_id: int | None = None,
        on_status=None,  # Optional[Callable[[str], Awaitable[None]]]
    ) -> PrepareResult:
        """Prepare everything needed for LLM call: skill matching, knowledge injection,
        prompt compilation, and message list assembly.

        If a short-circuit result is produced (rule engine / data query / input eval),
        it is stored in PrepareResult.early_return and the caller should return it directly.

        force_skill_id: 沙盒测试模式，直接使用指定 skill（允许 draft，跳过匹配和权限检查，
                        仅允许调用本人创建的 skill）。
        on_status: 可选回调，每个子阶段完成时被调用，传入阶段名字符串。
        """

        async def _emit(stage: str):
            if on_status:
                await on_status(stage)
        default_config = llm_gateway.resolve_config(db, "skill.execute")

        # Load workspace if present
        workspace = None
        workspace_skills: list[Skill] = []
        if conversation.workspace_id:
            try:
                from app.models.workspace import Workspace
                workspace = db.get(Workspace, conversation.workspace_id)
                if workspace:
                    workspace_skills = [
                        db.get(Skill, wsk.skill_id)
                        for wsk in workspace.workspace_skills
                        if db.get(Skill, wsk.skill_id) is not None
                    ]
                    if active_skill_ids is not None:
                        workspace_skills = [s for s in workspace_skills if s.id in active_skill_ids]
            except Exception as e:
                logger.warning(f"Workspace load failed: {e}")

        # 1. Skill matching
        import time as _time
        _phase_t0 = _time.monotonic()
        await _emit("matching_skill")
        skill = None

        # 沙盒模式：直接使用指定 skill，跳过所有匹配逻辑（允许 draft，仅限本人）
        if force_skill_id is not None:
            forced = db.get(Skill, force_skill_id)
            if forced and (user_id is None or forced.created_by == user_id):
                skill = forced
                if conversation.skill_id != skill.id:
                    conversation.skill_id = skill.id
                    db.flush()
                # 沙盒模式直接跳到后续准备阶段，不做 skill 切换/权限检查
                skill_version = skill.versions[0] if skill and skill.versions else None
                model_config_id = skill_version.model_config_id if skill_version else None
                if not model_config_id and workspace and getattr(workspace, "model_config_id", None):
                    model_config_id = workspace.model_config_id
                model_config = llm_gateway.resolve_config(db, "skill.execute", model_config_id)
                _check_model_grant(db, model_config, user_id)

                messages = (
                    db.query(Message)
                    .filter(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at)
                    .all()
                )

                # 编译 prompt 并返回
                if skill_version:
                    base_prompt = self._inject_templates(skill_version.system_prompt or "")
                    structured_ctx = self._get_latest_structured_output(messages)
                    system_content = prompt_compiler.compile(
                        system_prompt=base_prompt,
                        output_schema=skill_version.output_schema,
                        extracted_vars={},
                        structured_context=structured_ctx,
                    )
                else:
                    system_content = _DEFAULT_SYSTEM

                knowledge_ctx = await self._inject_knowledge(
                    user_message, skill, db=db, user_id=user_id,
                )
                if knowledge_ctx:
                    system_content += f"\n\n## 参考知识\n\n{knowledge_ctx}"

                # 构建消息列表（同主流程）
                raw_pairs: list[tuple] = []
                pending_user = None
                for m in messages:
                    if m.role == MessageRole.USER:
                        if pending_user is not None:
                            raw_pairs.append((pending_user, None))
                        pending_user = m
                    elif m.role == MessageRole.ASSISTANT:
                        asst_content = (m.content or "").strip()
                        if pending_user is not None and asst_content:
                            raw_pairs.append((pending_user, m))
                            pending_user = None

                llm_messages = [{"role": "system", "content": system_content}]
                for user_m, asst_m in raw_pairs:
                    if asst_m is None:
                        continue
                    _ac = (asst_m.content or "").strip()
                    if not _ac:
                        continue
                    llm_messages.append({"role": "user", "content": user_m.content or "(empty)"})
                    llm_messages.append({"role": "assistant", "content": _ac})
                llm_messages.append({"role": "user", "content": user_message})

                return PrepareResult(
                    llm_messages=llm_messages,
                    model_config=model_config,
                    skill_name=skill.name,
                    skill_id=skill.id,
                    skill_version=skill_version,
                    workspace=workspace,
                    available_tools=[],
                    tools_schema=[],
                )

        current_skill = db.get(Skill, conversation.skill_id) if conversation.skill_id else None

        # active_skill_ids 限制：若当前 skill 不在激活列表内，视为无 current_skill
        if current_skill and active_skill_ids is not None and current_skill.id not in active_skill_ids:
            current_skill = None

        if current_skill:
            switch_candidates = [s for s in (workspace_skills or []) if s.id != current_skill.id]
            if switch_candidates:
                try:
                    # 一次 LLM 调用完成切换判断 + 匹配
                    skill = await self._match_or_keep_skill(
                        db, current_skill, user_message, switch_candidates,
                    )
                except Exception as e:
                    logger.warning(f"Skill match_or_keep failed: {e}")
                    skill = current_skill
            else:
                skill = current_skill
        else:
            try:
                # 合并候选列表 = workspace skills + 全局 published（去重），一次匹配
                if workspace and workspace_skills:
                    global_skills = (
                        db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()
                    )
                    seen_ids = {s.id for s in workspace_skills}
                    merged = list(workspace_skills) + [s for s in global_skills if s.id not in seen_ids]
                    # active_skill_ids 进一步过滤（workspace 加载时已过滤过 workspace_skills，此处处理 global_skills 混入的情况）
                    if active_skill_ids is not None:
                        merged = [s for s in merged if s.id in active_skill_ids]
                    skill = await self._match_skill(
                        db, user_message, default_config,
                        candidate_skills=merged,
                    )
                else:
                    # 无 workspace：加载个人工作台配置中已挂载的 skill
                    _user_config = None
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
                        skill = await self._match_skill(
                            db, user_message, default_config,
                            candidate_skills=candidates,
                        )
                    elif _personal_skills:
                        skill = await self._match_skill(
                            db, user_message, default_config,
                            candidate_skills=_personal_skills,
                        )
                    else:
                        skill = await self._match_skill(db, user_message, default_config)
            except Exception as e:
                logger.warning(f"Skill matching failed: {e}")
                skill = None

        # ── 权限校验：callable 检查 ──
        if skill and user_id:
            try:
                from app.services.permission_engine import permission_engine
                caller = db.get(User, user_id) if user_id else None
                if caller and not permission_engine.check_skill_callable(caller, skill.id, db):
                    logger.info(f"Skill {skill.name} not callable for user {user_id}, skipping")
                    skill = None
            except Exception as e:
                logger.warning(f"Callable check failed: {e}")

        logger.info(f"[prepare] Phase A (skill matching) done in {_time.monotonic()-_phase_t0:.2f}s → skill={skill.name if skill else None}")
        _phase_t0 = _time.monotonic()

        if skill and conversation.skill_id != skill.id:
            conversation.skill_id = skill.id
            db.flush()

        # 2. Get conversation history
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
            .all()
        )

        # 3. Get model config
        skill_version = skill.versions[0] if skill and skill.versions else None
        model_config_id = skill_version.model_config_id if skill_version else None
        if not model_config_id and workspace and getattr(workspace, "model_config_id", None):
            model_config_id = workspace.model_config_id
        model_config = llm_gateway.resolve_config(db, "skill.execute", model_config_id)
        _check_model_grant(db, model_config, user_id)

        # Helper to build early PrepareResult
        def _early(result: tuple[str, dict]) -> PrepareResult:
            return PrepareResult(
                llm_messages=[], model_config=model_config,
                skill_name=skill.name if skill else None,
                skill_id=skill.id if skill else None,
                skill_version=skill_version, workspace=workspace,
                early_return=result,
            )

        # 4a. Structured mode: try rule engine first（同步逻辑，不阻塞，保持原位）
        if skill and skill.mode == SkillMode.STRUCTURED:
            try:
                from app.services.rule_engine import rule_engine
                rule_result = await rule_engine.try_evaluate(
                    db, skill, user_message, default_config
                )
                if rule_result is not None:
                    return _early(rule_result)
            except Exception as e:
                logger.warning(f"Rule engine failed, falling through to LLM: {e}")

        # Phase B: 并行化 — data_intent / input_eval / knowledge / vars / tool_chain
        # 这五个任务都只依赖 skill（来自 Phase A），彼此无依赖，可以全部并行。
        await _emit("checking_context")

        # 加载工具列表（同步，无 LLM，先取出来供 tool_chain 并行任务使用）
        available_tools = []
        try:
            from app.services.tool_executor import tool_executor
            if workspace and hasattr(workspace, 'workspace_tools') and workspace.workspace_tools:
                from app.models.tool import ToolRegistry
                available_tools = [
                    db.get(ToolRegistry, wt.tool_id)
                    for wt in workspace.workspace_tools
                    if db.get(ToolRegistry, wt.tool_id) is not None
                ]
            elif skill:
                available_tools = tool_executor.get_tools_for_skill(db, skill.id)
        except Exception as e:
            logger.warning(f"Tool loading for chain detection failed: {e}")

        # ---------- 并行任务定义 ----------

        async def _task_data_intent():
            """4b. Data queries: 规则前置 → LLM 意图分类"""
            if not (skill and skill.data_queries):
                return None
            _t = _time.monotonic()
            try:
                from app.services.data_engine import data_engine
                intent = data_engine.classify_intent_fast(user_message)
                if intent is None:
                    intent = await data_engine.classify_intent(user_message, default_config)
                return intent
            except Exception as e:
                logger.warning(f"Intent classification failed: {e}")
                return None
            finally:
                logger.debug(f"[prepare] _task_data_intent done in {_time.monotonic()-_t:.2f}s")

        async def _task_input_eval():
            """4c. InputEvaluator: 检查用户输入是否足够"""
            if not (skill_version and skill_version.required_inputs):
                return None
            _t = _time.monotonic()
            n_required = len(skill_version.required_inputs)
            max_clarify_msgs = n_required * 2
            if len(messages) > max_clarify_msgs:
                return None  # 超出轮次上限，强制放行
            try:
                from app.services.input_evaluator import input_evaluator
                return await input_evaluator.evaluate(
                    purpose=skill.description or skill.name,
                    required_inputs=skill_version.required_inputs,
                    history_messages=messages,
                    current_message="",
                )
            except Exception as e:
                logger.warning(f"InputEvaluator failed: {e}")
                return None
            finally:
                logger.debug(f"[prepare] _task_input_eval done in {_time.monotonic()-_t:.2f}s")

        async def _task_knowledge():
            """知识检索 + 精排"""
            _t = _time.monotonic()
            need_knowledge = not skill or (skill and skill.auto_inject)
            if not need_knowledge:
                return ""
            _project_id = getattr(conversation, "project_id", None)
            try:
                return await self._inject_knowledge(
                    user_message, skill, db=db, user_id=user_id, project_id=_project_id
                )
            finally:
                logger.debug(f"[prepare] _task_knowledge done in {_time.monotonic()-_t:.2f}s")

        async def _task_vars():
            """变量提取"""
            if not (skill_version and skill_version.variables):
                return {}
            _t = _time.monotonic()
            history_text = "\n".join(
                f"{m.role.value}: {m.content}" for m in messages
            )
            history_text += f"\nuser: {user_message}"
            try:
                return await self._extract_variables(
                    skill_version.variables, history_text, default_config
                )
            finally:
                logger.debug(f"[prepare] _task_vars done in {_time.monotonic()-_t:.2f}s")

        async def _task_tool_chain():
            """4d. 非 FC 模型：工具精选 + 意图检测（FC 模型直接跳过）"""
            if not available_tools:
                return None
            _t = _time.monotonic()
            _model_supports_fc = llm_gateway.supports_function_calling(model_config)
            if _model_supports_fc and len(available_tools) <= 15:
                return None  # 让主模型自己决定，跳过 lite LLM 检测
            try:
                selected_tools = await self._select_tools_for_message(
                    db, user_message, available_tools, default_config
                )
                tool_intent = await self._detect_tool_intent(db, user_message, selected_tools, default_config)
                return tool_intent
            except Exception as e:
                logger.warning(f"Tool chain detection failed: {e}")
                return None
            finally:
                logger.debug(f"[prepare] _task_tool_chain done in {_time.monotonic()-_t:.2f}s")

        # ---------- 并行执行 ----------
        (
            _data_intent_result,
            _input_eval_result,
            knowledge_context,
            extracted_vars,
            _tool_intent,
        ) = await asyncio.gather(
            _task_data_intent(),
            _task_input_eval(),
            _task_knowledge(),
            _task_vars(),
            _task_tool_chain(),
            return_exceptions=True,
        )

        # 安全取值（gather 可能返回 Exception）
        if isinstance(knowledge_context, Exception):
            logger.warning(f"knowledge failed: {knowledge_context}")
            knowledge_context = ""
        if isinstance(extracted_vars, Exception):
            logger.warning(f"vars failed: {extracted_vars}")
            extracted_vars = {}
        if isinstance(_data_intent_result, Exception):
            logger.warning(f"data_intent failed: {_data_intent_result}")
            _data_intent_result = None
        if isinstance(_input_eval_result, Exception):
            logger.warning(f"input_eval failed: {_input_eval_result}")
            _input_eval_result = None
        if isinstance(_tool_intent, Exception):
            logger.warning(f"tool_chain failed: {_tool_intent}")
            _tool_intent = None

        # ---------- 处理并行任务的 early_return ----------

        # 4b 结果：data intent
        if _data_intent_result is not None and skill and skill.data_queries:
            intent_type = _data_intent_result.get("type", "ai_generate")
            if intent_type in ("data_query", "data_mutation"):
                try:
                    result = await self._handle_data_operation(
                        db, skill, user_message, model_config, user_id, intent_type
                    )
                    return _early(result)
                except Exception as e:
                    logger.warning(f"Data operation failed, falling through to LLM: {e}")

        # 4c 结果：input eval
        if _input_eval_result is not None and not _input_eval_result.get("pass", True):
            questions = _input_eval_result.get("missing_questions", [])
            text = questions[0] if questions else "请提供更多信息。"
            return _early((text, {}))

        # 4d 结果：tool chain（非 FC 模型）
        if _tool_intent and available_tools:
            structured_ctx = self._get_latest_structured_output(messages)
            if structured_ctx:
                try:
                    from app.services.tool_executor import tool_executor
                    tool_params = await self._map_output_to_tool_input(
                        db, structured_ctx, _tool_intent, default_config
                    )
                    result = await tool_executor.execute_tool(
                        db, _tool_intent.name, tool_params, user_id
                    )
                    return _early(self._format_tool_result(result, _tool_intent))
                except Exception as e:
                    logger.warning(f"Tool chain mapping failed, falling through to LLM: {e}")

        # 5. Inject available tools prompt（同步，无 LLM）
        tool_prompt = ""
        try:
            from app.services.tool_executor import tool_executor
            if workspace and workspace.workspace_tools:
                from app.models.workspace import WorkspaceTool
                from app.models.tool import ToolRegistry
                ws_tools = [
                    db.get(ToolRegistry, wt.tool_id)
                    for wt in workspace.workspace_tools
                    if db.get(ToolRegistry, wt.tool_id) is not None
                ]
                if ws_tools:
                    tool_prompt = tool_executor.build_tool_list_prompt(ws_tools)
            elif skill:
                bound_tools = tool_executor.get_tools_for_skill(db, skill.id)
                if bound_tools:
                    tool_prompt = tool_executor.build_tool_list_prompt(bound_tools)
        except Exception as e:
            logger.warning(f"Tool loading failed: {e}")

        logger.info(f"[prepare] Phase B (parallel context) done in {_time.monotonic()-_phase_t0:.2f}s")

        # Phase C: 组装 prompt
        await _emit("compiling_prompt")

        # Build system prompt
        if skill_version:
            base_prompt = self._inject_templates(skill_version.system_prompt)

            structured_ctx = self._get_latest_structured_output(messages)

            system_content = prompt_compiler.compile(
                system_prompt=base_prompt,
                output_schema=skill_version.output_schema,
                extracted_vars=extracted_vars,
                structured_context=structured_ctx,
            )
        else:
            # If workspace has system_context and no skill selected, use system_context as the
            # primary system prompt (replacing _DEFAULT_SYSTEM) so it's not overridden.
            if workspace and workspace.system_context:
                system_content = workspace.system_context
            else:
                system_content = _DEFAULT_SYSTEM

        if skill_version and workspace and workspace.system_context:
            # Skill selected: append workspace rules after skill's own prompt
            system_content += f"\n\n## 工作台附加指令\n\n{workspace.system_context}"

        # ── 个人工作台 Skill 路由 prompt 注入 ──
        if not workspace and user_id:
            try:
                from app.models.workspace import UserWorkspaceConfig
                _uwc = (
                    db.query(UserWorkspaceConfig)
                    .filter(UserWorkspaceConfig.user_id == user_id)
                    .first()
                )
                if _uwc:
                    if _uwc.needs_prompt_refresh:
                        await self._refresh_skill_routing_prompt(db, _uwc)
                    if _uwc.skill_routing_prompt:
                        system_content += f"\n\n{_uwc.skill_routing_prompt}"
            except Exception as e:
                logger.warning(f"Skill routing prompt injection failed: {e}")

        if workspace and getattr(workspace, "project_id", None):
            try:
                from app.models.project import ProjectContext
                other_contexts = (
                    db.query(ProjectContext)
                    .filter(
                        ProjectContext.project_id == workspace.project_id,
                        ProjectContext.workspace_id != workspace.id,
                    )
                    .all()
                )
                if other_contexts:
                    ctx_parts = []
                    for ctx in other_contexts:
                        ws_name = ctx.workspace.name if ctx.workspace else f"workspace#{ctx.workspace_id}"
                        if ctx.summary:
                            ctx_parts.append(f"**{ws_name}**: {ctx.summary}")
                    if ctx_parts:
                        project_ctx_text = "\n".join(ctx_parts)
                        system_content += f"\n\n## 项目团队进展（其他成员）\n\n{project_ctx_text}"
            except Exception as e:
                logger.warning(f"Project context injection failed: {e}")

        # ── 附属文件内容注入（example / knowledge-base / reference / template）──
        if skill and skill.source_files:
            _file_ctx = _read_source_files(skill.id, skill.source_files)
            if _file_ctx:
                system_content += _file_ctx

        if knowledge_context:
            system_content += f"\n\n## 参考知识\n\n{knowledge_context}"

        if tool_prompt:
            system_content += f"\n\n{tool_prompt}"

        # ── 权限：data_scope 注入 ──
        if skill and user_id:
            try:
                from app.services.permission_engine import permission_engine
                caller = db.get(User, user_id) if user_id else None
                if caller:
                    scope = permission_engine.get_data_scope(caller, skill.id, db)
                    if scope:
                        scope_lines = []
                        for domain, rule in scope.items():
                            if isinstance(rule, dict):
                                vis = rule.get("visibility", "none")
                                fields = rule.get("fields")
                                excluded = rule.get("excluded")
                                parts = [f"- {domain}: 可见范围={vis}"]
                                if fields:
                                    parts.append(f"可见字段={','.join(fields)}")
                                if excluded:
                                    parts.append(f"禁止字段={','.join(excluded)}")
                                scope_lines.append(" / ".join(parts))
                            else:
                                scope_lines.append(f"- {domain}: {rule}")
                        if scope_lines:
                            system_content += (
                                "\n\n## 数据权限约束（严格遵守，不可绕过）\n"
                                "以下是当前用户的数据访问范围，回答中不得涉及禁止字段，"
                                "不得推测或虚构权限范围外的数据。\n"
                                + "\n".join(scope_lines)
                            )
            except Exception as e:
                logger.warning(f"Data scope injection failed: {e}")

        # ── 数据表上下文注入（当 Skill 绑定了 data_queries 时）──
        if skill and skill.data_queries:
            try:
                from app.models.business import BusinessTable as BT
                from app.services.data_engine import data_engine as _de
                table_names = list({q.get("table_name") for q in skill.data_queries if q.get("table_name")})
                if table_names:
                    bts = db.query(BT).filter(BT.table_name.in_(table_names)).all()
                    if bts:
                        tables_for_ctx = []
                        for bt in bts:
                            cols = _de._get_columns(db, bt.table_name)
                            tables_for_ctx.append({
                                "table_name": bt.table_name,
                                "display_name": bt.display_name,
                                "description": bt.description or "",
                                "columns": cols,
                                "validation_rules": bt.validation_rules or {},
                                "workflow": bt.workflow or {},
                            })
                        table_ctx = _de._build_table_context(tables_for_ctx)
                        system_content += f"\n\n## 可用数据表\n\n{table_ctx}"
            except Exception as e:
                logger.warning(f"Data table context injection failed: {e}")

        # 追加通用行为约束（仅当 system_content 中没有自带约束时）
        if "回复规范" not in system_content and "禁止行为" not in system_content:
            system_content += (
                "\n\n## 重要提醒\n"
                "- 直接回答，不要重复用户的问题\n"
                "- 不要以「好的」「当然」等词开头\n"
                "- 如果调用了工具，基于结果给出清晰回复，不要展示原始 JSON\n"
            )

        # 8. Build message list for LLM
        # Skip assistant messages with empty content — they indicate failed/aborted tool-call rounds
        # and would cause API errors (e.g. Kimi rejects empty content in history).
        # Also drop any user message that has no following assistant reply (orphaned turns).
        raw_pairs: list[tuple] = []  # (user_msg, assistant_msg_or_None)
        pending_user = None
        for m in messages:
            if m.role == MessageRole.USER:
                if pending_user is not None:
                    raw_pairs.append((pending_user, None))
                pending_user = m
            elif m.role == MessageRole.ASSISTANT:
                assistant_content = (m.content or "").strip()
                if pending_user is not None and assistant_content:
                    raw_pairs.append((pending_user, m))
                    pending_user = None
                # else: empty assistant reply → drop both turns to avoid poisoning context

        llm_messages = [{"role": "system", "content": system_content}]
        for user_m, asst_m in raw_pairs:
            if asst_m is None:
                continue  # orphaned user turn with no valid reply — skip entirely
            _ac = (asst_m.content or "").strip()
            if not _ac:
                continue
            llm_messages.append({"role": "user", "content": user_m.content or "(empty)"})
            llm_messages.append({"role": "assistant", "content": _ac})
        llm_messages.append({"role": "user", "content": user_message})

        # 9. Context window compaction
        llm_messages = await self._compact_if_needed(db, llm_messages, model_config)

        return PrepareResult(
            llm_messages=llm_messages,
            model_config=model_config,
            skill_name=skill.name if skill else None,
            skill_id=skill.id if skill else None,
            skill_version=skill_version,
            workspace=workspace,
            available_tools=available_tools,
            tools_schema=self._build_tools_schema(available_tools) if available_tools else [],
        )

    async def _compact_if_needed(
        self,
        db: Session,
        llm_messages: list[dict],
        model_config: dict,
        threshold: float = 0.85,
    ) -> list[dict]:
        """If estimated token usage exceeds threshold * context_window, summarize early history."""
        # Simple estimate: 1 token ≈ 2 Chinese chars ≈ 4 bytes
        total_chars = sum(len(m.get("content") or "") for m in llm_messages)
        estimated_tokens = total_chars // 2

        context_window = model_config.get("context_window", 32000)
        if estimated_tokens <= context_window * threshold:
            return llm_messages

        # Split: keep system + last 6 turns, summarize the rest
        system_msgs = [m for m in llm_messages if m["role"] == "system"]
        non_system = [m for m in llm_messages if m["role"] != "system"]

        keep_recent = 6  # rounds × 2 messages
        early = non_system[:-keep_recent] if len(non_system) > keep_recent else []
        recent = non_system[-keep_recent:]

        if not early:
            return llm_messages

        try:
            summary = await self._summarize_history(db, early, model_config)
            compacted = [
                *system_msgs,
                {"role": "user", "content": f"[系统消息：前期对话摘要，非用户发言]\n{summary}"},
                *recent,
            ]
            new_chars = sum(len(m.get("content") or "") for m in compacted)
            logger.info(
                f"Context compacted: {total_chars} → {new_chars} chars "
                f"(~{estimated_tokens} → {new_chars // 2} tokens)"
            )
            return compacted
        except Exception as e:
            logger.warning(f"Context compaction failed, using original: {e}")
            return llm_messages

    async def _summarize_history(self, db: Session, messages: list[dict], model_config: dict) -> str:
        """Summarize a list of messages into a concise paragraph."""
        history_text = "\n".join(
            f"{m['role']}: {m.get('content', '')[:500]}" for m in messages
        )
        prompt = (
            "请用简洁的中文（200字以内）总结以下对话的主要内容和结论。\n"
            "要求：\n"
            "1. 必须保留用户的原始请求/目标（第一条 user 消息的核心意图）\n"
            "2. 保留关键信息、重要结果和已完成的步骤\n"
            "3. 如有工具调用，保留工具名称和关键结果\n\n"
            f"{history_text}"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.compress_history")
        except Exception:
            lite_config = model_config
        result, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        return result.strip()

    async def execute(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        user_id: int | None = None,
        active_skill_ids: list[int] | None = None,
        force_skill_id: int | None = None,
    ) -> tuple[str, dict]:
        prep = await self.prepare(db, conversation, user_message, user_id, active_skill_ids, force_skill_id=force_skill_id)

        # Short-circuit: prepare already produced a final result
        if prep.early_return is not None:
            return prep.early_return

        # 9. Call LLM（传入 tools_schema，支持 native function calling）
        response, llm_usage = await llm_gateway.chat(
            model_config=prep.model_config,
            messages=prep.llm_messages,
            tools=prep.tools_schema or None,
        )

        # 10. Structured output
        skill_version = prep.skill_version
        structured_output = None
        if skill_version and skill_version.output_schema:
            parsed = self._try_parse_structured_output(response)
            if parsed is not None:
                structured_output = parsed
                response = prompt_compiler.render_structured_as_markdown(
                    skill_version.output_schema, parsed
                )

        # 11. Agent Loop: detect and execute tool calls
        tool_meta: dict = {}
        if "```tool_call" in response:
            skill = db.get(Skill, prep.skill_id) if prep.skill_id else None
            response, tool_meta = await self._handle_tool_calls(
                db, skill, response, prep.llm_messages, prep.model_config, user_id,
                tools_schema=prep.tools_schema,
            )

        # 12. Auto-execute python-pptx code blocks
        if prep.skill_name == "pptx-generation" and "```python" in response:
            tool_meta = self._execute_pptx_code(response)

        # 13. Auto-execute HTML PPT generation
        if prep.skill_name == "pptx-generation" and not tool_meta and "```html" in response:
            tool_meta = self._execute_html_ppt(response)

        if structured_output is not None:
            tool_meta["structured_output"] = structured_output

        tool_meta["llm_usage"] = llm_usage

        # ── 权限：输出侧 output_mask ──
        if structured_output and prep.skill_id and user_id:
            try:
                from app.services.permission_engine import permission_engine
                from app.models.permission import SkillPolicy
                caller = db.get(User, user_id)
                if caller:
                    # 尝试根据 Skill 关联的数据域做 output_mask
                    policy = db.query(SkillPolicy).filter(
                        SkillPolicy.skill_id == prep.skill_id
                    ).first()
                    if policy and policy.default_data_scope:
                        # 对每个涉及的 data_domain 做 mask
                        for domain_key, domain_conf in (policy.default_data_scope or {}).items():
                            if isinstance(domain_conf, dict) and domain_conf.get("data_domain_id"):
                                masked = permission_engine.apply_output_masks(
                                    user=caller,
                                    data=structured_output,
                                    data_domain_id=domain_conf["data_domain_id"],
                                    db=db,
                                )
                                structured_output = masked
                        tool_meta["structured_output"] = structured_output
            except Exception as e:
                logger.warning(f"Output mask failed: {e}")

        return response, tool_meta

    async def _handle_tool_calls(
        self,
        db: Session,
        skill: Skill,
        response: str,
        llm_messages: list[dict],
        model_config: dict,
        user_id: int | None,
        max_rounds: int = 5,
        tools_schema: list[dict] | None = None,
        native_tool_calls: list[dict] | None = None,
    ) -> tuple[str, dict]:
        """Execute tool calls and continue conversation.

        Returns (response_text, extra_meta) where extra_meta may contain download_url etc.
        """
        extra_meta: dict = {}
        async for item in self._handle_tool_calls_stream(
            db, skill, response, llm_messages, model_config, user_id, max_rounds,
            tools_schema=tools_schema, native_tool_calls=native_tool_calls,
        ):
            if isinstance(item, tuple):
                response, extra_meta = item
        return response, extra_meta

    async def _execute_tools_parallel(
        self,
        db: Session,
        tool_calls: list[dict],
        user_id: int | None,
    ) -> list[tuple[dict, dict]]:
        """并行执行所有工具调用，返回 [(call, result), ...]。"""
        from app.services.tool_executor import tool_executor

        async def _exec_one(call: dict) -> tuple[dict, dict]:
            tool_name = call.get("tool") or call.get("name", "")
            raw_args = call.get("params") or call.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    params = json.loads(raw_args)
                except json.JSONDecodeError:
                    params = {}
            else:
                params = raw_args
            result = await tool_executor.execute_tool(db, tool_name, params, user_id)
            return call, result

        return list(await asyncio.gather(*[_exec_one(c) for c in tool_calls]))

    async def _handle_tool_calls_stream(
        self,
        db: Session,
        skill,
        response: str,
        llm_messages: list[dict],
        model_config: dict,
        user_id: int | None,
        max_rounds: int = 5,
        tools_schema: list[dict] | None = None,
        native_tool_calls: list[dict] | None = None,
        start_block_idx: int = 1,
        thinking_content: str = "",
    ):
        """Streaming version: yields SSE-ready dicts for tool_call events, then yields (response, meta) at end.

        支持两种工具调用模式：
        - native_tool_calls 非空：原生 function calling（模型支持时）
        - 否则：解析 response 中的 ```tool_call``` 文本块（fallback）
        """
        extra_meta: dict = {}
        block_idx = start_block_idx  # caller tracks how many blocks were already emitted
        consecutive_failures = 0  # 连续失败计数，用于早停

        # 提取原始用户请求用于目标复述（注意力操纵，防止多轮工具调用中任务漂移）
        original_user_request = ""
        for m in reversed(llm_messages):
            if m.get("role") == "user":
                original_user_request = (m.get("content") or "")[:200]
                break

        for round_num in range(max_rounds):
            # 解析本轮工具调用列表
            if native_tool_calls:
                # 原生 function calling：直接使用结构化数据（每轮都优先用，不只第0轮）
                calls = native_tool_calls
                use_native = True
            else:
                # 文本 fallback：从 response 解析 ```tool_call``` 块
                pattern = r"```tool_call\s*(.*?)\s*```"
                raw_matches = re.findall(pattern, response, re.DOTALL)
                calls = []
                for m in raw_matches:
                    try:
                        parsed = json.loads(m)
                        calls.append(parsed)
                    except json.JSONDecodeError:
                        pass
                use_native = False

            if not calls:
                break

            yield {"event": "round_start", "data": {"round": round_num + 1, "max_rounds": max_rounds}}

            # 并行触发所有工具（先发 start 事件，再并行执行）
            call_indices: dict[int, dict] = {}  # block_idx → call
            for call in calls:
                tool_name = call.get("tool") or call.get("name", "")
                raw_args = call.get("params") or call.get("arguments", {})
                params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                yield {"event": "content_block_start", "data": {
                    "index": block_idx, "type": "tool_call",
                    "tool": tool_name, "input": params,
                }}
                yield {"event": "tool_progress", "data": {
                    "index": block_idx, "message": f"校验参数...",
                    "phase": "validating",
                }}
                call_indices[block_idx] = call
                block_idx += 1

            # 并行执行
            pairs = await self._execute_tools_parallel(db, calls, user_id)

            # 收集结果并发送 stop 事件
            tool_results = []
            result_block_start = block_idx - len(calls)
            for i, (call, result) in enumerate(pairs):
                tool_name = call.get("tool") or call.get("name", "")
                ok = result.get("ok", False)
                duration_ms = result.get("duration_ms")

                if ok and isinstance(result.get("result"), dict):
                    tool_result_data = result["result"]
                    if "download_url" in tool_result_data:
                        extra_meta["download_url"] = tool_result_data["download_url"]
                    if "filename" in tool_result_data:
                        extra_meta["download_filename"] = tool_result_data["filename"]

                result_str = json.dumps(result, ensure_ascii=False, indent=2)
                yield {"event": "content_block_stop", "data": {
                    "index": result_block_start + i, "type": "tool_call",
                    "tool": tool_name, "result": result_str, "ok": ok,
                    "duration_ms": duration_ms,
                }}

                if ok:
                    tool_results.append(f"工具 `{tool_name}` 执行结果：\n```json\n{result_str}\n```")
                else:
                    # 构建富上下文错误，帮助 LLM 自我修正
                    raw_args = call.get("params") or call.get("arguments", {})
                    params_used = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    # 获取工具的 input_schema 用于提示
                    from app.services.tool_executor import tool_executor as _te
                    _tool_obj = None
                    try:
                        from app.models.tool import ToolRegistry as _TR
                        _tool_obj = db.query(_TR).filter(_TR.name == tool_name).first()
                    except Exception:
                        pass
                    try:
                        schema_hint = json.dumps(_tool_obj.input_schema or {}, ensure_ascii=False) if _tool_obj else "{}"
                    except Exception:
                        schema_hint = "{}"
                    error_context = (
                        f"工具 `{tool_name}` 执行失败。\n"
                        f"错误信息：{result.get('error')}\n"
                        f"传入参数：{json.dumps(params_used, ensure_ascii=False)}\n"
                        f"工具期望的参数格式：{schema_hint}\n"
                        f"请检查参数并重试，或换一种方式完成用户的需求。"
                    )
                    tool_results.append(error_context)

            if not tool_results:
                yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": False}}
                break

            # 连续失败早停：所有工具都失败时计数+1，有任何成功则重置
            all_failed = all(not r.get("ok", False) for _, r in pairs)
            if all_failed:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= 2:
                logger.warning(f"Agent loop early stop: {consecutive_failures} consecutive all-fail rounds")
                yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": False, "reason": "consecutive_failures"}}
                break

            tool_result_text = "\n\n".join(tool_results)

            # 构建下一轮 messages
            if use_native:
                # 原生 function calling：使用规范的 tool / tool role 消息格式
                tool_calls_msg: list[dict] = []
                for call in calls:
                    raw_args = call.get("params") or call.get("arguments", {})
                    tool_calls_msg.append({
                        "id": call.get("id", f"call_{call.get('name', '')}"),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False),
                        },
                    })
                asst_msg: dict = {"role": "assistant", "content": None, "tool_calls": tool_calls_msg}
                # thinking 模型要求 assistant 消息必须带 reasoning_content
                if thinking_content:
                    asst_msg["reasoning_content"] = thinking_content
                llm_messages.append(asst_msg)
                thinking_content = ""  # 消费后清空，避免重复附加
                for call, result in pairs:
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", f"call_{call.get('name', '')}"),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                # 文本 fallback：将结果作为 user 消息追加
                # 目标复述：多轮时追加原始用户请求，防止任务漂移
                goal_reminder = ""
                if round_num >= 1 and original_user_request:
                    goal_reminder = f"\n\n[提醒] 用户的原始请求是：{original_user_request}"
                llm_messages.append({"role": "assistant", "content": response or "(calling tools)"})
                llm_messages.append({
                    "role": "user",
                    "content": f"[工具执行结果]\n\n{tool_result_text}\n\n请基于以上工具结果，给出最终回复。不需要重复展示JSON，直接告知用户结果即可。{goal_reminder}",
                })

            # 流式下一轮 LLM 响应
            yield {"event": "content_block_start", "data": {"index": block_idx, "type": "text"}}
            new_response = ""
            next_native_calls: list[dict] = []
            next_thinking_content = ""

            async for chunk_type, chunk_data in llm_gateway.chat_stream_typed(
                model_config=model_config,
                messages=llm_messages,
                tools=tools_schema if use_native else None,
            ):
                if chunk_type == "tool_call":
                    next_native_calls.append(chunk_data)
                elif chunk_type == "thinking":
                    next_thinking_content += chunk_data
                elif chunk_type == "content":
                    new_response += chunk_data
                    yield {"event": "content_block_delta", "data": {"index": block_idx, "delta": {"text": chunk_data}}}
                    yield {"event": "delta", "data": {"text": chunk_data}}

            yield {"event": "content_block_stop", "data": {"index": block_idx}}
            block_idx += 1
            response = new_response

            has_next = bool(next_native_calls) or "```tool_call" in response
            yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": has_next}}

            if not has_next:
                break

            # 下一轮使用原生 tool_calls（若有），并传入本轮收集的 thinking
            native_tool_calls = next_native_calls if next_native_calls else None
            thinking_content = next_thinking_content

        response = re.sub(r"```tool_call\s*.*?\s*```", "", response, flags=re.DOTALL).strip()
        yield (response, extra_meta)


    # ── Structured output helpers ──────────────────────────────────

    @staticmethod
    def _get_latest_structured_output(messages: list[Message]) -> dict | None:
        """Find the most recent structured_output from conversation history."""
        for msg in reversed(messages):
            if msg.role == MessageRole.ASSISTANT and msg.metadata_:
                so = msg.metadata_.get("structured_output")
                if so:
                    return so
        return None

    @staticmethod
    def _try_parse_structured_output(response: str) -> dict | None:
        """Attempt to parse LLM response as JSON structured output.

        Handles both raw JSON and JSON wrapped in ```json blocks.
        """
        text = response.strip()
        # Strip ```json ... ``` wrapper if present
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    async def _detect_tool_intent(
        self,
        db: Session,
        user_message: str,
        available_tools: list,
        model_config: dict,
    ) -> object | None:
        """Detect if user wants to invoke a specific tool (e.g. '做成PPT', '生成Excel')."""
        if not available_tools:
            return None

        tool_list = "\n".join(
            f"- {t.name}: {t.display_name} — {t.description or '无描述'}"
            for t in available_tools
        )
        prompt = (
            "判断用户是否想调用以下某个工具。如果是，返回工具name；否则返回 none。\n"
            "只返回工具name或none，不要其他内容。\n\n"
            f"可用工具:\n{tool_list}\n\n"
            f"用户消息: {user_message}"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.tool_match")
        except Exception:
            lite_config = model_config
        result, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=50,
        )
        name = result.strip().splitlines()[0].strip().strip('"').strip("'")
        if name.lower() == "none":
            return None
        for t in available_tools:
            if t.name == name:
                return t
        return None

    async def _extract_tool_params(
        self,
        db: Session,
        user_message: str,
        messages: list,
        tool,
        model_config: dict,
    ) -> dict | None:
        """从用户消息和对话历史中直接提取工具参数（不依赖 structured_output）。
        若参数不足以调用工具，返回 None（让 LLM 通过对话补全）。
        """
        schema = tool.input_schema or {}
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        required_fields = schema.get("required", [])

        # 组装近期对话上下文（最近6条）
        recent = messages[-6:] if len(messages) > 6 else messages
        history_text = "\n".join(
            f"{m.role.value}: {m.content[:300]}" for m in recent
            if hasattr(m, "role") and hasattr(m, "content")
        )

        prompt = (
            f"从以下对话中提取调用工具 `{tool.name}` 所需的参数。\n\n"
            f"工具参数 Schema：\n```json\n{schema_str}\n```\n\n"
            f"对话历史：\n{history_text}\n\n"
            f"用户当前消息：{user_message}\n\n"
            f"必填参数：{required_fields}\n\n"
            "只返回 JSON 对象（符合 Schema），如果必填参数无法从对话中确定，返回 null。"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.tool_param_extract")
        except Exception:
            lite_config = model_config
        result, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1000,
        )
        raw = result.strip()
        if raw.lower() == "null":
            return None
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def _select_tools_for_message(
        self,
        db: Session,
        user_message: str,
        available_tools: list,
        model_config: dict,
    ) -> list:
        """当工具超过 5 个时，用 lite 模型精选最相关的 3-5 个工具。"""
        if len(available_tools) <= 5:
            return available_tools

        tool_list = "\n".join(
            f"- {t.name}: {t.display_name} — {t.description or '无描述'}"
            for t in available_tools
        )
        prompt = (
            f"用户消息：{user_message}\n\n"
            f"可用工具列表：\n{tool_list}\n\n"
            "从以上工具中选出最相关的 3-5 个（返回 tool name，逗号分隔），只返回名称，不要其他内容。"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.tool_select")
        except Exception:
            lite_config = model_config
        try:
            result, _ = await llm_gateway.chat(
                model_config=lite_config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=100,
            )
            selected_names = {n.strip().strip('"').strip("'") for n in result.split(",")}
            selected = [t for t in available_tools if t.name in selected_names]
            return selected if selected else available_tools[:5]
        except Exception as e:
            logger.warning(f"Tool selection failed, using first 5: {e}")
            return available_tools[:5]

    async def _map_output_to_tool_input(
        self,
        db: Session,
        structured_output: dict,
        tool,
        model_config: dict,
    ) -> dict:
        """Use LLM to map a structured output to a tool's input_schema."""
        tool_schema = json.dumps(tool.input_schema or {}, ensure_ascii=False, indent=2)
        data_json = json.dumps(structured_output, ensure_ascii=False, indent=2)
        prompt = (
            f"你有以下结构化数据：\n```json\n{data_json}\n```\n\n"
            f"目标工具 `{tool.name}` 的 input_schema：\n```json\n{tool_schema}\n```\n\n"
            "请将数据映射为工具需要的参数格式。只返回JSON，不要其他内容。"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.tool_output_map")
        except Exception:
            lite_config = model_config
        result, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = re.sub(r"```(?:json)?|```", "", result).strip()
        return json.loads(raw)

    @staticmethod
    def _format_tool_result(result: dict, tool) -> tuple[str, dict]:
        """Format tool execution result for display + metadata."""
        meta: dict = {}
        if result.get("ok") and isinstance(result.get("result"), dict):
            data = result["result"]
            if "download_url" in data:
                meta["download_url"] = data["download_url"]
            if "filename" in data:
                meta["download_filename"] = data["filename"]

        if result.get("ok"):
            content = f"已使用工具 **{tool.display_name}** 完成操作。"
            if meta.get("download_url"):
                content += f"\n\n文件已生成，点击下载。"
        else:
            content = f"工具 `{tool.name}` 执行失败：{result.get('error', '未知错误')}"

        return content, meta

    def _inject_templates(self, system_prompt: str) -> str:
        """Replace {{TEMPLATE_CLASSES}} with dynamically read CSS class reference from template files."""
        if "{{TEMPLATE_CLASSES}}" not in system_prompt:
            return system_prompt

        from pathlib import Path
        tmpl_dir = Path(__file__).parent.parent / "tools" / "ppt_templates"
        parts = []
        for tmpl_path in sorted(tmpl_dir.glob("*.html")):
            name = tmpl_path.stem
            html = tmpl_path.read_text(encoding="utf-8")
            # Extract only the <style> block
            m = re.search(r"<style>(.*?)</style>", html, re.DOTALL | re.IGNORECASE)
            if not m:
                continue
            css = m.group(1)
            # Extract class names only (e.g. .sketch-box, .flat-card)
            class_names = re.findall(r"\.([\w-]+)\s*\{", css)
            unique = list(dict.fromkeys(class_names))  # preserve order, dedupe
            parts.append(f"### {name} 模板可用 class\n" + ", ".join(f".{c}" for c in unique))

        injected = "\n\n".join(parts) if parts else "（模板文件未找到）"
        return system_prompt.replace("{{TEMPLATE_CLASSES}}", injected)

    def _execute_pptx_code(self, response: str) -> dict:
        """Extract python code block from LLM response and execute it to generate a pptx file."""
        import os
        import uuid
        import subprocess
        import tempfile
        from pathlib import Path

        # Extract first ```python ... ``` block
        pattern = r"```python\s*(.*?)\s*```"
        matches = re.findall(pattern, response, re.DOTALL)
        if not matches:
            return {}

        code = matches[0]

        # Ensure output goes to uploads/generated/
        upload_dir = os.environ.get("UPLOAD_DIR", "./uploads")
        generated_dir = Path(upload_dir) / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)

        file_id = f"{uuid.uuid4().hex}.pptx"
        file_path = generated_dir / file_id

        # Inject output path into code: replace any prs.save(...) with our path
        if "prs.save(" in code:
            code = re.sub(r'prs\.save\([^)]+\)', f'prs.save("{file_path}")', code)
        else:
            code += f'\nprs.save("{file_path}")\n'

        try:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(code)
                tmp_path = f.name

            result = subprocess.run(
                ["python3", tmp_path],
                capture_output=True, text=True, timeout=120
            )
            os.unlink(tmp_path)

            if result.returncode != 0:
                logger.error(f"pptx code execution failed: {result.stderr}")
                return {}

            if not file_path.exists():
                return {}

            return {
                "download_url": f"/api/files/{file_id}",
                "download_filename": "演示文稿.pptx",
            }
        except Exception as e:
            logger.error(f"pptx code execution error: {e}")
            return {}


    def _execute_html_ppt(self, response: str) -> dict:
        """Extract HTML slides block from LLM response and generate HTML PPT file.

        Expects LLM to output:
          TEMPLATE: sketch|flat
          TITLE: 标题

          ```html
          <slides html here>
          ```
        """
        import re as _re

        # Extract template hint (line starting with TEMPLATE:)
        template_match = _re.search(r"TEMPLATE:\s*(\w+)", response, _re.IGNORECASE)
        template = template_match.group(1).lower() if template_match else "flat"

        # Extract title hint
        title_match = _re.search(r"TITLE:\s*(.+)", response, _re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else "演示文稿"

        # Extract ```html ... ``` block
        html_match = _re.search(r"```html\s*(.*?)\s*```", response, _re.DOTALL)
        if not html_match:
            return {}

        slides_html = html_match.group(1)

        from app.tools.html_ppt_generator import execute as ppt_execute
        result = ppt_execute({"template": template, "title": title, "slides_html": slides_html})
        if "download_url" not in result:
            logger.error(f"html_ppt_generator failed: {result.get('error')}")
            return {}

        return {
            "download_url": result["download_url"],
            "download_filename": result.get("filename", "演示文稿.html"),
        }


skill_engine = SkillEngine()
