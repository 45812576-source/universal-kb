"""项目引擎：LLM规划生成、workspace 创建、上下文同步、报告生成。"""
from __future__ import annotations

import datetime
import json
import logging

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_GENERATE_PLAN_PROMPT = """你是企业项目规划助手。根据项目背景和各成员的分工描述，为每个成员设计专属 workspace 配置。

项目名称: {project_name}
项目背景: {project_description}

成员分工:
{members_info}

请为每个成员返回 JSON 格式的 workspace 配置。只返回 JSON，不要其他内容。格式如下：
{{
  "overall_flow": "并行 / 串行描述",
  "workspaces": [
    {{
      "user_id": 成员用户ID,
      "workspace_name": "workspace名称",
      "identity_desc": "角色身份描述（如：视觉设计师）",
      "system_context": "workspace系统提示（包含角色职责、项目背景、工作重点）",
      "responsibilities": ["职责1", "职责2"],
      "suggested_skills": ["skill名称1", "skill名称2"],
      "suggested_tools": ["tool名称1"],
      "task_order": 0,
      "dependencies": []
    }}
  ]
}}

task_order: 0表示并行，正整数表示串行顺序（1先于2）。
dependencies: 依赖的其他成员 user_id 列表。"""

_SYNC_CONTEXT_PROMPT = """请根据以下对话内容，生成一段简洁的工作进度摘要（200字以内），用于共享给项目其他成员了解进展。

workspace名称: {workspace_name}
成员角色: {role_desc}

最近对话（最多20轮）:
{conversation_text}

只返回摘要文本，不要其他内容。"""

_GENERATE_REPORT_PROMPT = """请根据以下项目信息生成一份{report_type_label}报告。

项目名称: {project_name}
项目背景: {project_description}
报告周期: {period_start} 至 {period_end}

各成员工作进展:
{contexts_text}

请生成一份结构清晰的{report_type_label}报告，包含：整体进展、各成员进展、存在问题、下一步计划。"""


class ProjectEngine:

    async def generate_plan(
        self,
        project_name: str,
        project_description: str,
        members: list[dict],  # [{user_id, display_name, role_desc}]
        db: Session,
    ) -> dict:
        """调用 LLM 生成项目 workspace 规划。"""
        members_info = "\n".join(
            f"- 用户ID {m['user_id']} ({m['display_name']}): {m['role_desc']}"
            for m in members
        )
        prompt = _GENERATE_PLAN_PROMPT.format(
            project_name=project_name,
            project_description=project_description,
            members_info=members_info,
        )
        model_config = llm_gateway.get_config(db)
        response, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4096,
        )
        # 解析 JSON
        import re
        text = response.strip()
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"generate_plan JSON parse failed: {text[:200]}")
            raise ValueError("LLM返回格式错误，请重试")

    async def apply_plan(
        self,
        project,
        plan: dict,
        db: Session,
    ) -> None:
        """根据规划方案创建 workspace 并绑定到成员。"""
        from app.models.workspace import Workspace, WorkspaceSkill, WorkspaceTool, WorkspaceStatus
        from app.models.skill import Skill, SkillStatus
        from app.models.tool import ToolRegistry
        from app.models.project import ProjectMember

        workspaces_plan = plan.get("workspaces", [])

        for ws_plan in workspaces_plan:
            user_id = ws_plan.get("user_id")
            if not user_id:
                continue

            # 创建 Workspace
            ws = Workspace(
                name=ws_plan.get("workspace_name", "项目工作台"),
                description=ws_plan.get("identity_desc", ""),
                icon="chat",
                color="#00D1FF",
                category="项目",
                status=WorkspaceStatus.PUBLISHED,
                created_by=user_id,
                department_id=project.department_id,
                visibility="department",
                welcome_message="你好，有什么可以帮你的？",
                system_context=ws_plan.get("system_context", ""),
                project_id=project.id,
            )
            db.add(ws)
            db.flush()

            # 绑定推荐 skill（按名称模糊匹配）
            for skill_name in ws_plan.get("suggested_skills", []):
                skill = db.query(Skill).filter(
                    Skill.name.ilike(f"%{skill_name}%"),
                    Skill.status == SkillStatus.PUBLISHED,
                ).first()
                if skill:
                    db.add(WorkspaceSkill(workspace_id=ws.id, skill_id=skill.id))

            # 绑定推荐 tool（按名称模糊匹配）
            for tool_name in ws_plan.get("suggested_tools", []):
                tool = db.query(ToolRegistry).filter(
                    ToolRegistry.name.ilike(f"%{tool_name}%"),
                    ToolRegistry.is_active == True,
                ).first()
                if tool:
                    db.add(WorkspaceTool(workspace_id=ws.id, tool_id=tool.id))

            # 创建或更新 ProjectMember
            member = db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == user_id,
            ).first()
            if member:
                member.workspace_id = ws.id
                member.task_order = ws_plan.get("task_order", 0)
            else:
                db.add(ProjectMember(
                    project_id=project.id,
                    user_id=user_id,
                    role_desc=ws_plan.get("identity_desc", ""),
                    workspace_id=ws.id,
                    task_order=ws_plan.get("task_order", 0),
                ))

        db.commit()

    async def sync_context(self, project, db: Session) -> None:
        """为每个项目 workspace 生成进度摘要并存入 project_contexts。"""
        from app.models.project import ProjectMember, ProjectContext
        from app.models.conversation import Conversation, Message

        model_config = llm_gateway.get_config(db)

        for member in project.members:
            if not member.workspace_id:
                continue

            # 获取该 workspace 最近20条对话消息
            conv = db.query(Conversation).filter(
                Conversation.workspace_id == member.workspace_id,
                Conversation.is_active == True,
            ).order_by(Conversation.updated_at.desc()).first()

            if not conv:
                continue

            messages = (
                db.query(Message)
                .filter(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(20)
                .all()
            )
            if not messages:
                continue

            messages = list(reversed(messages))
            conversation_text = "\n".join(
                f"{m.role.value}: {m.content[:200]}" for m in messages
            )

            prompt = _SYNC_CONTEXT_PROMPT.format(
                workspace_name=member.workspace.name if member.workspace else "未知",
                role_desc=member.role_desc or "项目成员",
                conversation_text=conversation_text,
            )

            try:
                summary, _ = await llm_gateway.chat(
                    model_config=model_config,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=400,
                )
            except Exception as e:
                logger.warning(f"sync_context LLM failed for workspace {member.workspace_id}: {e}")
                continue

            # 更新或创建 ProjectContext
            ctx = db.query(ProjectContext).filter(
                ProjectContext.project_id == project.id,
                ProjectContext.workspace_id == member.workspace_id,
            ).first()
            if ctx:
                ctx.summary = summary.strip()
                ctx.updated_at = datetime.datetime.utcnow()
            else:
                db.add(ProjectContext(
                    project_id=project.id,
                    workspace_id=member.workspace_id,
                    summary=summary.strip(),
                ))

        db.commit()

    async def generate_report(
        self,
        project,
        report_type: str,  # "daily" | "weekly"
        db: Session,
    ) -> str:
        """根据所有 workspace 压缩上下文生成日/周报。"""
        from app.models.project import ProjectContext, ProjectReport, ReportType

        contexts = db.query(ProjectContext).filter(
            ProjectContext.project_id == project.id
        ).all()

        contexts_text = ""
        for ctx in contexts:
            ws_name = ctx.workspace.name if ctx.workspace else f"workspace#{ctx.workspace_id}"
            contexts_text += f"\n### {ws_name}\n{ctx.summary or '暂无进展'}\n"

        today = datetime.date.today()
        if report_type == "daily":
            period_start = today
            period_end = today
            report_type_label = "日报"
        else:
            # weekly: 本周开始到今天
            period_start = today - datetime.timedelta(days=today.weekday())
            period_end = today
            report_type_label = "周报"

        prompt = _GENERATE_REPORT_PROMPT.format(
            report_type_label=report_type_label,
            project_name=project.name,
            project_description=project.description or "",
            period_start=str(period_start),
            period_end=str(period_end),
            contexts_text=contexts_text or "暂无成员工作进展。",
        )

        model_config = llm_gateway.get_config(db)
        content, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2000,
        )

        # 存入数据库
        report = ProjectReport(
            project_id=project.id,
            report_type=ReportType(report_type),
            content=content.strip(),
            period_start=period_start,
            period_end=period_end,
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        return content.strip()


project_engine = ProjectEngine()
