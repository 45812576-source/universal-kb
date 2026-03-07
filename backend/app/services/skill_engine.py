"""Skill dispatch engine: intent matching → variable extraction → knowledge injection → LLM call."""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageRole
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.skill import Skill, SkillStatus
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_SKILL_MATCH_PROMPT = """你是意图识别系统。根据用户消息从可用Skill中选择最匹配的一个。
若没有合适的Skill返回字符串 "none"。只返回Skill的name，不要其他内容。

可用Skills:
{skill_list}

用户消息: {user_message}"""

_PARAM_EXTRACT_PROMPT = """从对话中提取以下变量的值。若某变量无法确定，值设为 null。
只返回JSON对象，不要其他内容。

需要提取的变量: {variables}
对话内容:
{conversation}"""

_DEFAULT_SYSTEM = (
    "你是企业知识助手。根据提供的参考知识回答用户问题。"
    "如果引用了知识，请在回答末尾标注「参考知识」。"
    "如果知识不足以回答，请如实说明。"
)


class SkillEngine:

    async def _match_skill(
        self, db: Session, user_message: str, model_config: dict
    ) -> Skill | None:
        skills = (
            db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()
        )
        if not skills:
            return None

        skill_list = "\n".join(
            f"- {s.name}: {s.description or '无描述'}" for s in skills
        )
        prompt = _SKILL_MATCH_PROMPT.format(
            skill_list=skill_list, user_message=user_message
        )
        result = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=50,
        )
        name = result.strip().strip('"').strip("'")
        if name.lower() == "none":
            return None
        return db.query(Skill).filter(Skill.name == name).first()

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
        result = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        try:
            return json.loads(result.strip())
        except json.JSONDecodeError:
            return {}

    def _inject_knowledge(self, query: str, skill: Skill | None) -> str:
        """Retrieve relevant knowledge chunks from Milvus and format as context."""
        try:
            from app.services.vector_service import search_knowledge
            hits = search_knowledge(query, top_k=6)
        except Exception as e:
            logger.warning(f"Knowledge search failed: {e}")
            return ""

        if not hits:
            return ""

        # Deduplicate by knowledge_id and fetch titles from DB
        parts = []
        seen_ids = set()
        for h in hits:
            if h["knowledge_id"] not in seen_ids:
                seen_ids.add(h["knowledge_id"])
                parts.append(f"[相关知识 score={h['score']}]\n{h['text']}")

        return "\n\n---\n\n".join(parts)

    async def execute(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
    ) -> str:
        # Get default model config for intent matching
        default_config = llm_gateway.get_config(db)

        # 1. Match Skill on first message (or if not yet matched)
        if not conversation.skill_id:
            try:
                skill = await self._match_skill(db, user_message, default_config)
                if skill:
                    conversation.skill_id = skill.id
                    db.flush()
            except Exception as e:
                logger.warning(f"Skill matching failed: {e}")
                skill = None
        else:
            skill = db.get(Skill, conversation.skill_id)

        # 2. Get conversation history
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
            .all()
        )

        # 3. Get model config (from skill version or default)
        skill_version = skill.versions[0] if skill and skill.versions else None
        model_config_id = skill_version.model_config_id if skill_version else None
        model_config = llm_gateway.get_config(db, model_config_id)

        # 4. Inject knowledge
        knowledge_context = ""
        if not skill or (skill and skill.auto_inject):
            knowledge_context = self._inject_knowledge(user_message, skill)

        # 5. Build system prompt
        if skill_version:
            system_content = skill_version.system_prompt
            # Extract and substitute variables
            if skill_version.variables:
                history_text = "\n".join(
                    f"{m.role.value}: {m.content}" for m in messages
                )
                history_text += f"\nuser: {user_message}"
                try:
                    extracted = await self._extract_variables(
                        skill_version.variables, history_text, default_config
                    )
                    for var, val in extracted.items():
                        if val:
                            # variables are stored like "{industry}", match with braces
                            system_content = system_content.replace(
                                "{" + var.strip("{}") + "}", str(val)
                            )
                except Exception as e:
                    logger.warning(f"Variable extraction failed: {e}")
        else:
            system_content = _DEFAULT_SYSTEM

        if knowledge_context:
            system_content += f"\n\n## 参考知识\n\n{knowledge_context}"

        # 6. Build message list for LLM
        llm_messages = [{"role": "system", "content": system_content}]
        for m in messages:
            if m.role in (MessageRole.USER, MessageRole.ASSISTANT):
                llm_messages.append({"role": m.role.value, "content": m.content})
        llm_messages.append({"role": "user", "content": user_message})

        # 7. Call LLM
        response = await llm_gateway.chat(
            model_config=model_config,
            messages=llm_messages,
        )
        return response


skill_engine = SkillEngine()
