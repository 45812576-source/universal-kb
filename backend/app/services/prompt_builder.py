"""PromptBuilder — system prompt 编译、模板注入，从 skill_engine 抽出。"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.conversation import Message, MessageRole
from app.services import prompt_compiler

logger = logging.getLogger(__name__)

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


class PromptBuilder:
    """System prompt 编译与组装。"""

    @property
    def default_system(self) -> str:
        return _DEFAULT_SYSTEM

    def inject_templates(self, system_prompt: str) -> str:
        """Replace {{TEMPLATE_CLASSES}} with dynamically read CSS class reference from template files."""
        if "{{TEMPLATE_CLASSES}}" not in system_prompt:
            return system_prompt

        from pathlib import Path
        tmpl_dir = Path(__file__).parent.parent / "tools" / "ppt_templates"
        parts = []
        for tmpl_path in sorted(tmpl_dir.glob("*.html")):
            name = tmpl_path.stem
            html = tmpl_path.read_text(encoding="utf-8")
            m = re.search(r"<style>(.*?)</style>", html, re.DOTALL | re.IGNORECASE)
            if not m:
                continue
            css = m.group(1)
            class_names = re.findall(r"\.([\w-]+)\s*\{", css)
            unique = list(dict.fromkeys(class_names))
            parts.append(f"### {name} 模板可用 class\n" + ", ".join(f".{c}" for c in unique))

        injected = "\n\n".join(parts) if parts else "（模板文件未找到）"
        return system_prompt.replace("{{TEMPLATE_CLASSES}}", injected)

    def compile_system_prompt(
        self,
        *,
        skill_version,
        workspace,
        messages: list[Message],
        extracted_vars: dict,
        knowledge_context: str,
        tool_prompt: str,
        user_id: int | None,
        db: Session,
    ) -> str:
        """编译完整的 system prompt，整合 skill prompt、workspace context、
        知识注入、工具列表、权限约束、数据表上下文等。"""

        # 基础 prompt
        if skill_version:
            base_prompt = self.inject_templates(skill_version.system_prompt or "")
            structured_ctx = self._get_latest_structured_output(messages)
            system_content = prompt_compiler.compile(
                system_prompt=base_prompt,
                output_schema=skill_version.output_schema,
                extracted_vars=extracted_vars,
                structured_context=structured_ctx,
            )
        else:
            if workspace and workspace.system_context:
                system_content = workspace.system_context
            else:
                system_content = _DEFAULT_SYSTEM

        # Workspace 附加指令
        if skill_version and workspace and workspace.system_context:
            system_content += f"\n\n## 工作台附加指令\n\n{workspace.system_context}"

        # 个人工作台 Skill 路由 prompt 注入
        if not workspace and user_id:
            system_content = self._inject_routing_prompt(db, user_id, system_content)

        # 项目团队上下文
        if workspace and getattr(workspace, "project_id", None):
            system_content = self._inject_project_context(db, workspace, system_content)

        # 附属文件内容注入
        skill = skill_version.skill if skill_version and hasattr(skill_version, 'skill') else None
        if not skill and skill_version:
            # fallback: 通过 skill_version 的关系取 skill
            try:
                skill = skill_version.skill
            except Exception:
                pass

        # 知识注入
        if knowledge_context:
            system_content += f"\n\n## 参考知识\n\n{knowledge_context}"

        # 工具列表
        if tool_prompt:
            system_content += f"\n\n{tool_prompt}"

        # 权限：data_scope 注入
        if skill_version and user_id:
            skill_id = skill_version.skill_id if hasattr(skill_version, 'skill_id') else None
            if skill_id:
                system_content = self._inject_data_scope(db, user_id, skill_id, system_content)

        # 数据表上下文
        if skill_version:
            skill_obj = None
            try:
                from app.models.skill import Skill
                skill_obj = db.get(Skill, skill_version.skill_id) if hasattr(skill_version, 'skill_id') else None
            except Exception:
                pass
            if skill_obj and skill_obj.data_queries:
                system_content = self._inject_data_table_context(db, skill_obj, system_content)

        # 通用行为约束
        if "回复规范" not in system_content and "禁止行为" not in system_content:
            system_content += (
                "\n\n## 重要提醒\n"
                "- 直接回答，不要重复用户的问题\n"
                "- 不要以「好的」「当然」等词开头\n"
                "- 如果调用了工具，基于结果给出清晰回复，不要展示原始 JSON\n"
            )

        return system_content

    def _inject_routing_prompt(self, db: Session, user_id: int, system_content: str) -> str:
        """注入个人工作台 Skill 路由 prompt。"""
        try:
            from app.models.workspace import UserWorkspaceConfig
            from app.services.skill_router import skill_router
            _uwc = (
                db.query(UserWorkspaceConfig)
                .filter(UserWorkspaceConfig.user_id == user_id)
                .first()
            )
            if _uwc:
                # 注意：routing prompt 刷新是异步的，这里不做同步调用
                # 在 runtime 层 prepare 阶段已经刷新
                if _uwc.skill_routing_prompt:
                    system_content += f"\n\n{_uwc.skill_routing_prompt}"
        except Exception as e:
            logger.warning(f"Skill routing prompt injection failed: {e}")
        return system_content

    def _inject_project_context(self, db: Session, workspace, system_content: str) -> str:
        """注入项目团队其他成员的进展。"""
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
        return system_content

    def _inject_data_scope(self, db: Session, user_id: int, skill_id: int, system_content: str) -> str:
        """注入数据权限约束。"""
        try:
            from app.services.permission_engine import permission_engine
            from app.models.user import User
            caller = db.get(User, user_id)
            if caller:
                scope = permission_engine.get_data_scope(caller, skill_id, db)
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
        return system_content

    def _inject_data_table_context(self, db: Session, skill, system_content: str) -> str:
        """注入数据表上下文。"""
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
        return system_content

    @staticmethod
    def _get_latest_structured_output(messages: list[Message]) -> dict | None:
        """Find the most recent structured_output from conversation history."""
        for msg in reversed(messages):
            if msg.role == MessageRole.ASSISTANT and msg.metadata_:
                so = msg.metadata_.get("structured_output")
                if so:
                    return so
        return None


prompt_builder = PromptBuilder()
