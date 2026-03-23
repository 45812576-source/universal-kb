"""项目引擎：LLM规划生成、workspace 创建、上下文同步、报告生成。"""
from __future__ import annotations

import datetime
import json
import logging
import re

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

_EXTRACT_REQUIREMENTS_PROMPT = """你是需求分析专家。请从以下业务需求对话中，提取出结构化的需求信息，供开发人员实施。

项目名称: {project_name}
项目背景: {project_description}

需求方最近对话（最多30轮）:
{conversation_text}

请返回 JSON 格式，只返回 JSON，不要其他内容：
{{
  "requirements": "需求摘要（结构化的功能点列表，每个功能点单独一行，用数字编号）",
  "acceptance_criteria": "验收标准（每条单独一行，明确可验证的条件）"
}}"""

_DAILY_SUMMARY_PROMPT = """你是项目知识沉淀助手。请根据以下项目聊天记录和未完成任务，生成今日项目进展摘要。

项目名称: {project_name}
日期: {today}

今日对话记录:
{conversation_text}

未完成任务:
{tasks_text}

请返回如下格式的摘要（只返回摘要内容，不要其他说明）：

## {today}
**主要进展**
（用2-4句话概括今天的讨论重点和推进事项）

**待跟进**:
- [ ] 待办事项1
- [ ] 待办事项2
（列出需要跟进的行动项，若无则写"暂无"）"""

_TODO_REMINDER_PROMPT = """你是项目跟进助手。请根据以下项目昨日/近期进展，生成一条简短的早间跟进提醒（不超过100字），鼓励团队跟进待办事项。

项目名称: {project_name}
近期进展摘要:
{summary_text}

只返回提醒消息文本，不要其他内容。"""

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

    async def extract_requirements(
        self,
        project,
        workspace_id: int,
        db: Session,
    ) -> dict:
        """从 chat workspace 对话中提取需求摘要和验收标准，存入 ProjectContext。"""
        from app.models.project import ProjectContext
        from app.models.conversation import Conversation, Message

        # 获取该 workspace 最近30条消息
        conv = db.query(Conversation).filter(
            Conversation.workspace_id == workspace_id,
            Conversation.is_active == True,
        ).order_by(Conversation.updated_at.desc()).first()

        if not conv:
            raise ValueError("该 workspace 暂无对话记录")

        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .limit(30)
            .all()
        )
        if not messages:
            raise ValueError("该 workspace 暂无消息")

        messages = list(reversed(messages))
        conversation_text = "\n".join(
            f"{m.role.value}: {m.content[:300]}" for m in messages
        )

        prompt = _EXTRACT_REQUIREMENTS_PROMPT.format(
            project_name=project.name,
            project_description=project.description or "",
            conversation_text=conversation_text,
        )

        model_config = llm_gateway.get_config(db)
        response, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )

        text = response.strip()
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"extract_requirements JSON parse failed: {text[:200]}")
            raise ValueError("LLM返回格式错误，请重试")

        requirements = result.get("requirements", "")
        acceptance_criteria = result.get("acceptance_criteria", "")

        # 更新或创建 ProjectContext（chat workspace 对应的那条）
        ctx = db.query(ProjectContext).filter(
            ProjectContext.project_id == project.id,
            ProjectContext.workspace_id == workspace_id,
        ).first()
        if ctx:
            ctx.requirements = requirements
            ctx.acceptance_criteria = acceptance_criteria
            ctx.handoff_status = "submitted"
            ctx.handoff_at = datetime.datetime.utcnow()
        else:
            db.add(ProjectContext(
                project_id=project.id,
                workspace_id=workspace_id,
                requirements=requirements,
                acceptance_criteria=acceptance_criteria,
                handoff_status="submitted",
                handoff_at=datetime.datetime.utcnow(),
            ))
        db.commit()

        return {"requirements": requirements, "acceptance_criteria": acceptance_criteria}

    async def apply_dev_template(
        self,
        project,
        requester_user_id: int,
        developer_user_id: int,
        db: Session,
    ) -> dict:
        """为 dev 类型项目创建固定的 chat + opencode workspace。"""
        from app.models.workspace import Workspace, WorkspaceStatus
        from app.models.project import ProjectMember

        # 创建需求方 chat workspace
        chat_ws = Workspace(
            name=f"{project.name} · 需求工作台",
            description="业务需求讨论与确认",
            icon="chat",
            color="#00D1FF",
            category="项目",
            status=WorkspaceStatus.PUBLISHED,
            created_by=requester_user_id,
            department_id=project.department_id,
            visibility="department",
            welcome_message="你好！请描述你的需求，我会帮你整理成开发可执行的规格。",
            system_context=f"你是 {project.name} 项目的需求分析助手。帮助业务方澄清、整理和确认功能需求。项目背景：{project.description or ''}",
            project_id=project.id,
            workspace_type="chat",
        )
        db.add(chat_ws)
        db.flush()

        # 创建开发方 opencode workspace
        dev_ws = Workspace(
            name=f"{project.name} · 开发工作台",
            description="代码实施与开发",
            icon="code",
            color="#6B46C1",
            category="项目",
            status=WorkspaceStatus.PUBLISHED,
            created_by=developer_user_id,
            department_id=project.department_id,
            visibility="department",
            welcome_message="",
            system_context=f"项目：{project.name}\n背景：{project.description or ''}\n\n（需求将在需求交接后自动注入此处）",
            project_id=project.id,
            workspace_type="opencode",
        )
        db.add(dev_ws)
        db.flush()

        # 创建 ProjectMember 记录
        # 需求方
        req_member = db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == requester_user_id,
        ).first()
        if req_member:
            req_member.workspace_id = chat_ws.id
            req_member.role_desc = "需求定义"
        else:
            db.add(ProjectMember(
                project_id=project.id,
                user_id=requester_user_id,
                role_desc="需求定义",
                workspace_id=chat_ws.id,
                task_order=1,
            ))

        # 开发方
        dev_member = db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == developer_user_id,
        ).first()
        if dev_member:
            dev_member.workspace_id = dev_ws.id
            dev_member.role_desc = "代码实施"
        else:
            db.add(ProjectMember(
                project_id=project.id,
                user_id=developer_user_id,
                role_desc="代码实施",
                workspace_id=dev_ws.id,
                task_order=2,
            ))

        db.commit()
        return {"chat_workspace_id": chat_ws.id, "dev_workspace_id": dev_ws.id}

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

    async def daily_project_summary(self, project, db: Session) -> str:
        """汇总当日项目对话，生成 summary 追加到专属知识条目。"""
        from app.models.conversation import Conversation, Message
        from app.models.task import Task, TaskStatus
        from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage
        from app.models.project import ProjectMember, ProjectKnowledgeShare
        from app.services.knowledge_service import submit_knowledge
        from app.services.vector_service import index_knowledge, delete_knowledge_vectors

        today = datetime.date.today()
        since = datetime.datetime.combine(today, datetime.time.min)

        # 查今日项目所有对话消息（最多100条）
        convs = (
            db.query(Conversation)
            .filter(Conversation.project_id == project.id, Conversation.is_active == True)
            .all()
        )
        conv_ids = [c.id for c in convs]

        messages = []
        if conv_ids:
            messages = (
                db.query(Message)
                .filter(
                    Message.conversation_id.in_(conv_ids),
                    Message.created_at >= since,
                )
                .order_by(Message.created_at.asc())
                .limit(100)
                .all()
            )

        if not messages:
            logger.info(f"daily_summary: no messages today for project {project.id}, skipping")
            return ""

        conversation_text = "\n".join(
            f"{m.role.value}: {m.content[:300]}" for m in messages
        )

        # 查未完成任务
        pending_tasks = (
            db.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
            )
            .all()
        )
        tasks_text = "\n".join(
            f"- [{t.status.value}] {t.title}" for t in pending_tasks
        ) or "暂无"

        prompt = _DAILY_SUMMARY_PROMPT.format(
            project_name=project.name,
            today=str(today),
            conversation_text=conversation_text,
            tasks_text=tasks_text,
        )

        model_config = llm_gateway.get_config(db)
        summary_text, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        summary_text = summary_text.strip()
        # 截取 ## YYYY-MM-DD 开头的正式段落，丢弃 LLM 的推理过程
        _match = re.search(r'(## \d{4}-\d{2}-\d{2}.+)', summary_text, re.DOTALL)
        if _match:
            summary_text = _match.group(1).strip()

        # 查找或创建 KnowledgeEntry
        entry = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.source_type == "project_chat_log",
                KnowledgeEntry.source_file == str(project.id),
            )
            .first()
        )

        cutoff = today - datetime.timedelta(days=7)
        cutoff_str = str(cutoff)

        if entry:
            # 追加今日段落，裁掉7天前的段落
            existing = entry.content or ""
            # 按 ## YYYY-MM-DD 分割段落
            parts = re.split(r'(?=## \d{4}-\d{2}-\d{2})', existing)
            kept = [p for p in parts if p.strip() and p.strip()[3:13] >= cutoff_str]
            new_content = "\n\n".join(kept).strip()
            if new_content:
                new_content += "\n\n" + summary_text
            else:
                new_content = summary_text
            entry.content = new_content
            entry.updated_at = datetime.datetime.utcnow()
            db.flush()
            # 重新向量化
            try:
                delete_knowledge_vectors(entry.id)
                milvus_ids = index_knowledge(entry.id, entry.content, created_by=entry.created_by or 0)
                entry.milvus_ids = milvus_ids
            except Exception as e:
                logger.warning(f"daily_summary re-vectorize failed for entry {entry.id}: {e}")
            db.commit()
        else:
            # 创建新条目
            entry = KnowledgeEntry(
                title=f"{project.name} · 项目对话日志",
                content=summary_text,
                category="experience",
                source_type="project_chat_log",
                source_file=str(project.id),
                capture_mode="chat_delegate_confirmed",
                department_id=project.department_id,
                created_by=project.created_by if hasattr(project, "created_by") else None,
                visibility_scope="project",
            )
            db.add(entry)
            db.flush()
            submit_knowledge(db, entry)

        # 确保所有项目成员有 ProjectKnowledgeShare（防重复）
        members = db.query(ProjectMember).filter(ProjectMember.project_id == project.id).all()
        existing_shares = {
            (s.user_id, s.knowledge_id)
            for s in db.query(ProjectKnowledgeShare).filter(
                ProjectKnowledgeShare.project_id == project.id,
                ProjectKnowledgeShare.knowledge_id == entry.id,
            ).all()
        }
        for m in members:
            if (m.user_id, entry.id) not in existing_shares:
                db.add(ProjectKnowledgeShare(
                    project_id=project.id,
                    user_id=m.user_id,
                    knowledge_id=entry.id,
                ))
        db.commit()

        logger.info(f"daily_summary done for project {project.id}, entry {entry.id}")
        return summary_text

    async def inject_todo_reminder(self, project, db: Session) -> None:
        """向项目所有 active 对话注入早间 todo 提醒消息。"""
        from app.models.knowledge import KnowledgeEntry
        from app.models.conversation import Conversation, Message, MessageRole

        today = datetime.date.today()
        today_str = str(today)

        # 查项目专属知识条目
        entry = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.source_type == "project_chat_log",
                KnowledgeEntry.source_file == str(project.id),
            )
            .first()
        )
        if not entry or not entry.content:
            logger.info(f"inject_todo_reminder: no knowledge entry for project {project.id}, skipping")
            return

        # 取最近一个段落作为 summary
        parts = re.split(r'(?=## \d{4}-\d{2}-\d{2})', entry.content)
        recent_parts = [p.strip() for p in parts if p.strip()]
        summary_text = recent_parts[-1] if recent_parts else entry.content[:500]

        prompt = _TODO_REMINDER_PROMPT.format(
            project_name=project.name,
            summary_text=summary_text[:1000],
        )

        model_config = llm_gateway.get_config(db)
        reminder_text, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=200,
        )
        reminder_text = reminder_text.strip()

        # 查该项目所有 active conversations
        convs = (
            db.query(Conversation)
            .filter(Conversation.project_id == project.id, Conversation.is_active == True)
            .all()
        )

        injected = 0
        for conv in convs:
            # 防重：同一天同一 conversation 不重复注入
            already = (
                db.query(Message)
                .filter(
                    Message.conversation_id == conv.id,
                    Message.metadata_["source"].as_string() == "todo_reminder",
                    Message.metadata_["date"].as_string() == today_str,
                )
                .first()
            )
            if already:
                continue

            db.add(Message(
                conversation_id=conv.id,
                role=MessageRole.ASSISTANT,
                content=reminder_text,
                metadata_={"source": "todo_reminder", "date": today_str},
            ))
            injected += 1

        db.commit()
        logger.info(f"inject_todo_reminder: injected {injected} messages for project {project.id}")


project_engine = ProjectEngine()
