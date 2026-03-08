"""Skill dispatch engine: intent matching → variable extraction → knowledge injection → LLM call."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageRole
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.skill import Skill, SkillMode, SkillStatus
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
        if len(skills) == 1:
            return skills[0]

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
        # search within candidates first, then globally
        for s in skills:
            if s.name == name:
                return s
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

        parts = []
        seen_ids = set()
        for h in hits:
            if h["knowledge_id"] not in seen_ids:
                seen_ids.add(h["knowledge_id"])
                parts.append(f"[相关知识 score={h['score']}]\n{h['text']}")

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
            table_str = data_engine.format_results(rows, columns)
            return f"{explanation}\n\n{table_str}" if explanation else table_str
        else:
            affected = exec_result.get("affected_rows", 0)
            return f"操作成功，影响 {affected} 行。\n\n{explanation}"

    async def execute(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        user_id: int | None = None,
    ) -> str:
        # Get default model config for intent matching
        default_config = llm_gateway.get_config(db)

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
            except Exception as e:
                logger.warning(f"Workspace load failed: {e}")

        # 1. Match Skill on first message (or if not yet matched)
        if not conversation.skill_id:
            try:
                if workspace and workspace_skills:
                    # Single skill → use directly; multiple → match within candidates
                    skill = await self._match_skill(
                        db, user_message, default_config,
                        candidate_skills=workspace_skills,
                    )
                else:
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

        # 4a. Structured mode: try rule engine first
        if skill and skill.mode == SkillMode.STRUCTURED:
            try:
                from app.services.rule_engine import rule_engine
                rule_result = await rule_engine.try_evaluate(
                    db, skill, user_message, default_config
                )
                if rule_result is not None:
                    return rule_result
            except Exception as e:
                logger.warning(f"Rule engine failed, falling through to LLM: {e}")

        # 4b. If skill has data_queries, classify intent and possibly route to data operation
        if skill and skill.data_queries:
            try:
                from app.services.data_engine import data_engine
                intent = await data_engine.classify_intent(user_message, default_config)
                intent_type = intent.get("type", "ai_generate")
                if intent_type in ("data_query", "data_mutation"):
                    return await self._handle_data_operation(
                        db, skill, user_message, model_config, user_id, intent_type
                    )
            except Exception as e:
                logger.warning(f"Intent classification failed, falling through to LLM: {e}")

        # 5. Inject available tools (workspace tools take priority over skill tools)
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

        # 6. Inject knowledge
        knowledge_context = ""
        if not skill or (skill and skill.auto_inject):
            knowledge_context = self._inject_knowledge(user_message, skill)

        # 7. Build system prompt
        if skill_version:
            system_content = skill_version.system_prompt
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
                            system_content = system_content.replace(
                                "{" + var.strip("{}") + "}", str(val)
                            )
                except Exception as e:
                    logger.warning(f"Variable extraction failed: {e}")
        else:
            system_content = _DEFAULT_SYSTEM

        if workspace and workspace.system_context:
            system_content += f"\n\n## 工作台附加指令\n\n{workspace.system_context}"

        if knowledge_context:
            system_content += f"\n\n## 参考知识\n\n{knowledge_context}"

        if tool_prompt:
            system_content += f"\n\n{tool_prompt}"

        # 8. Build message list for LLM
        llm_messages = [{"role": "system", "content": system_content}]
        for m in messages:
            if m.role in (MessageRole.USER, MessageRole.ASSISTANT):
                llm_messages.append({"role": m.role.value, "content": m.content})
        llm_messages.append({"role": "user", "content": user_message})

        # 9. Call LLM (with Agent Loop for tool calls)
        response = await llm_gateway.chat(
            model_config=model_config,
            messages=llm_messages,
        )

        # 10. Agent Loop: detect and execute tool calls
        if skill and "```tool_call" in response:
            response = await self._handle_tool_calls(
                db, skill, response, llm_messages, model_config, user_id
            )

        return response

    async def _handle_tool_calls(
        self,
        db: Session,
        skill: Skill,
        response: str,
        llm_messages: list[dict],
        model_config: dict,
        user_id: int | None,
        max_rounds: int = 3,
    ) -> str:
        """Execute tool calls found in LLM response and continue conversation."""
        from app.services.tool_executor import tool_executor

        for _ in range(max_rounds):
            # Extract tool_call blocks
            pattern = r"```tool_call\s*(.*?)\s*```"
            matches = re.findall(pattern, response, re.DOTALL)
            if not matches:
                break

            tool_results = []
            for match in matches:
                try:
                    call = json.loads(match)
                    tool_name = call.get("tool", "")
                    params = call.get("params", {})
                    result = await tool_executor.execute_tool(db, tool_name, params, user_id)
                    tool_results.append(
                        f"工具 `{tool_name}` 执行结果：\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```"
                    )
                except Exception as e:
                    tool_results.append(f"工具调用解析失败：{e}")

            if not tool_results:
                break

            # Continue the conversation with tool results
            tool_result_text = "\n\n".join(tool_results)
            llm_messages.append({"role": "assistant", "content": response})
            llm_messages.append({
                "role": "user",
                "content": f"[工具执行结果]\n\n{tool_result_text}\n\n请基于以上工具结果，给出最终回复。",
            })

            response = await llm_gateway.chat(
                model_config=model_config,
                messages=llm_messages,
            )

            # If no more tool calls, done
            if "```tool_call" not in response:
                break

        return response


skill_engine = SkillEngine()
