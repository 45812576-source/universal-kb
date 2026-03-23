"""项目模块 API 路由。"""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.project import (
    Project, ProjectMember, ProjectKnowledgeShare,
    ProjectReport, ProjectContext, ProjectStatus, ReportType,
)
from app.models.user import User, Role
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/projects", tags=["projects"])

MAX_MEMBERS = 5


# ─── 权限：按角色过滤项目字段 ─────────────────────────────────────────────────

def _filter_project_fields(data: dict, user: User, db: Session) -> dict:
    """根据用户岗位的 DataScopePolicy(project 域) 过滤项目返回字段。

    super_admin / dept_admin / 无 position → 不过滤。
    有 position 但没配 project 域 → 不过滤（宽松策略）。
    output_mask 存的是 excluded 字段列表。
    """
    if user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        return data
    if not user.position_id:
        return data
    try:
        from app.models.permission import DataScopePolicy, DataDomain, PolicyResourceType
        # 找到 project 数据域
        project_domain = db.query(DataDomain).filter(DataDomain.name == "project").first()
        if not project_domain:
            return data
        scope = (
            db.query(DataScopePolicy)
            .filter(
                DataScopePolicy.target_position_id == user.position_id,
                DataScopePolicy.resource_type == PolicyResourceType.DATA_DOMAIN,
                DataScopePolicy.data_domain_id == project_domain.id,
            )
            .first()
        )
        if not scope or not scope.output_mask:
            return data
        excluded = set(scope.output_mask)
        return {k: v for k, v in data.items() if k not in excluded}
    except Exception:
        return data


# ─── Serialisation ────────────────────────────────────────────────────────────

def _member_dict(m: ProjectMember) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "display_name": m.user.display_name if m.user else None,
        "role_desc": m.role_desc,
        "workspace_id": m.workspace_id,
        "workspace_name": m.workspace.name if m.workspace else None,
        "task_order": m.task_order,
        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
    }


def _project_dict(p: Project, include_members: bool = False) -> dict:
    d = {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "status": p.status.value,
        "project_type": getattr(p, "project_type", "custom") or "custom",
        "owner_id": p.owner_id,
        "owner_name": p.owner.display_name if p.owner else None,
        "department_id": p.department_id,
        "max_members": p.max_members,
        "llm_generated_plan": p.llm_generated_plan,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
    if include_members:
        d["members"] = [_member_dict(m) for m in p.members]
    else:
        d["member_count"] = len(p.members)
        d["member_names"] = [m.user.display_name for m in p.members if m.user]
    return d


def _report_dict(r: ProjectReport) -> dict:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "report_type": r.report_type.value,
        "content": r.content,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end": r.period_end.isoformat() if r.period_end else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _context_dict(c: ProjectContext) -> dict:
    return {
        "id": c.id,
        "workspace_id": c.workspace_id,
        "workspace_name": c.workspace.name if c.workspace else None,
        "summary": c.summary,
        "requirements": getattr(c, "requirements", None),
        "acceptance_criteria": getattr(c, "acceptance_criteria", None),
        "handoff_status": getattr(c, "handoff_status", "none") or "none",
        "handoff_at": c.handoff_at.isoformat() if getattr(c, "handoff_at", None) else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ─── Schemas ──────────────────────────────────────────────────────────────────

class MemberInput(BaseModel):
    user_id: int
    role_desc: str


class DevMembersInput(BaseModel):
    requester_user_id: int  # 需求方
    developer_user_id: int  # 开发方


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    department_id: Optional[int] = None
    project_type: str = "custom"  # "dev" | "custom"
    members: list[MemberInput] = []
    dev_members: Optional[DevMembersInput] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    department_id: Optional[int] = None


class KnowledgeShareCreate(BaseModel):
    knowledge_id: int


# ─── Helper ───────────────────────────────────────────────────────────────────

def _get_project_or_404(project_id: int, db: Session) -> Project:
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


def _require_owner(project: Project, user: User) -> None:
    if project.owner_id != user.id and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "只有项目负责人可以执行此操作")


def _is_member_or_owner(project: Project, user: User) -> bool:
    if project.owner_id == user.id:
        return True
    if user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        return True
    return any(m.user_id == user.id for m in project.members)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("")
def create_project(
    req: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建项目。dev 类型自动创建固定 workspace，custom 类型走 LLM 规划流程。"""
    project_type = req.project_type if req.project_type in ("dev", "custom") else "custom"

    if project_type == "custom" and len(req.members) > MAX_MEMBERS:
        raise HTTPException(400, f"项目成员最多 {MAX_MEMBERS} 人")

    project = Project(
        name=req.name,
        description=req.description,
        owner_id=user.id,
        department_id=req.department_id or user.department_id,
        status=ProjectStatus.DRAFT,
        project_type=project_type,
    )
    db.add(project)
    db.flush()

    if project_type == "custom":
        for m in req.members:
            db.add(ProjectMember(
                project_id=project.id,
                user_id=m.user_id,
                role_desc=m.role_desc,
            ))

    db.commit()
    db.refresh(project)
    return _project_dict(project, include_members=True)


@router.get("")
def list_projects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取我参与的项目列表（作为负责人或成员）。"""
    # 作为负责人
    owned = db.query(Project).filter(
        Project.owner_id == user.id,
        Project.status != ProjectStatus.ARCHIVED,
    ).all()
    owned_ids = {p.id for p in owned}

    # 作为成员
    member_rows = db.query(ProjectMember).filter(ProjectMember.user_id == user.id).all()
    member_projects = [
        db.get(Project, row.project_id)
        for row in member_rows
        if row.project_id not in owned_ids
    ]
    member_projects = [p for p in member_projects if p and p.status != ProjectStatus.ARCHIVED]

    all_projects = owned + member_projects
    return [_filter_project_fields(_project_dict(p), user, db) for p in all_projects]


@router.get("/{project_id}")
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取项目详情（含成员、workspace、进度）。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权查看该项目")

    result = _project_dict(project, include_members=True)

    # 附加压缩上下文
    contexts = db.query(ProjectContext).filter(
        ProjectContext.project_id == project_id
    ).all()
    result["contexts"] = [_context_dict(c) for c in contexts]

    # 附加共享知识
    shares = db.query(ProjectKnowledgeShare).filter(
        ProjectKnowledgeShare.project_id == project_id
    ).all()
    result["knowledge_shares"] = [
        {
            "id": s.id,
            "user_id": s.user_id,
            "user_name": s.user.display_name if s.user else None,
            "knowledge_id": s.knowledge_id,
            "knowledge_title": s.knowledge.title if s.knowledge else None,
            "shared_at": s.shared_at.isoformat() if s.shared_at else None,
        }
        for s in shares
        if not (s.knowledge and s.knowledge.source_type == "project_chat_log")
    ]
    return _filter_project_fields(result, user, db)


@router.post("/{project_id}/generate")
async def generate_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """LLM 生成 workspace 规划（职责、skill、工具、流程）。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    if not project.members:
        raise HTTPException(400, "请先添加项目成员")

    from app.services.project_engine import project_engine

    members_input = []
    for m in project.members:
        members_input.append({
            "user_id": m.user_id,
            "display_name": m.user.display_name if m.user else f"user#{m.user_id}",
            "role_desc": m.role_desc or "项目成员",
        })

    plan = await project_engine.generate_plan(
        project_name=project.name,
        project_description=project.description or "",
        members=members_input,
        db=db,
    )

    project.llm_generated_plan = plan
    db.commit()
    return {"ok": True, "plan": plan}


@router.post("/{project_id}/apply-plan")
async def apply_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """负责人确认后，自动创建 workspace 并绑定 skill/工具。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    if not project.llm_generated_plan:
        raise HTTPException(400, "请先生成规划方案")

    from app.services.project_engine import project_engine
    await project_engine.apply_plan(project, project.llm_generated_plan, db)

    project.status = ProjectStatus.ACTIVE
    db.commit()
    db.refresh(project)
    return _project_dict(project, include_members=True)


@router.post("/{project_id}/apply-dev-template")
async def apply_dev_template(
    project_id: int,
    req: DevMembersInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """为 dev 项目创建固定的 chat + opencode workspace，并激活项目。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    if getattr(project, "project_type", "custom") != "dev":
        raise HTTPException(400, "仅 dev 类型项目可使用此端点")

    from app.services.project_engine import project_engine
    result = await project_engine.apply_dev_template(
        project=project,
        requester_user_id=req.requester_user_id,
        developer_user_id=req.developer_user_id,
        db=db,
    )

    project.status = ProjectStatus.ACTIVE
    db.commit()
    db.refresh(project)

    detail = _project_dict(project, include_members=True)
    detail.update(result)
    return detail


@router.post("/{project_id}/handoff")
async def submit_handoff(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从 chat workspace 提取需求，推送到 dev workspace 的 system_context。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权操作该项目")

    if getattr(project, "project_type", "custom") != "dev":
        raise HTTPException(400, "仅 dev 类型项目支持需求交接")

    # 找到 chat workspace（需求方成员）
    chat_member = next(
        (m for m in project.members if m.workspace and m.workspace.workspace_type == "chat"),
        None,
    )
    if not chat_member or not chat_member.workspace_id:
        raise HTTPException(400, "未找到需求方 workspace")

    # 找到 dev workspace（开发方成员）
    dev_member = next(
        (m for m in project.members if m.workspace and m.workspace.workspace_type == "opencode"),
        None,
    )

    from app.services.project_engine import project_engine
    result = await project_engine.extract_requirements(
        project=project,
        workspace_id=chat_member.workspace_id,
        db=db,
    )

    # 将需求注入到 dev workspace 的 system_context
    if dev_member and dev_member.workspace_id:
        dev_ws = db.get(Workspace, dev_member.workspace_id)
        if dev_ws:
            handoff_section = (
                f"\n\n---\n## 需求交接（来自业务方）\n\n"
                f"### 功能需求\n{result['requirements']}\n\n"
                f"### 验收标准\n{result['acceptance_criteria']}"
            )
            existing = dev_ws.system_context or ""
            # 避免重复追加：先去掉旧的交接段落
            import re
            existing = re.sub(r"\n\n---\n## 需求交接.*$", "", existing, flags=re.DOTALL)
            dev_ws.system_context = existing + handoff_section
            db.commit()

    return result


@router.get("/{project_id}/handoff")
def get_handoff(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看当前交接状态和内容。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权查看该项目")

    chat_member = next(
        (m for m in project.members if m.workspace and m.workspace.workspace_type == "chat"),
        None,
    )
    if not chat_member or not chat_member.workspace_id:
        return {"handoff_status": "none", "requirements": None, "acceptance_criteria": None}

    ctx = db.query(ProjectContext).filter(
        ProjectContext.project_id == project_id,
        ProjectContext.workspace_id == chat_member.workspace_id,
    ).first()

    if not ctx:
        return {"handoff_status": "none", "requirements": None, "acceptance_criteria": None}

    return {
        "handoff_status": getattr(ctx, "handoff_status", "none") or "none",
        "requirements": getattr(ctx, "requirements", None),
        "acceptance_criteria": getattr(ctx, "acceptance_criteria", None),
        "handoff_at": ctx.handoff_at.isoformat() if getattr(ctx, "handoff_at", None) else None,
    }


@router.get("/by-workspace/{workspace_id}")
def get_project_by_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """根据 workspace_id 反查所属项目及交接内容（用于 DevStudio 展示）。"""
    member = db.query(ProjectMember).filter(
        ProjectMember.workspace_id == workspace_id
    ).first()

    if not member:
        return None

    project = db.get(Project, member.project_id)
    if not project:
        return None

    if not _is_member_or_owner(project, user):
        return None

    if getattr(project, "project_type", "custom") != "dev":
        return None

    # 查找 chat workspace 的 context（含交接内容）
    chat_member = next(
        (m for m in project.members if m.workspace and m.workspace.workspace_type == "chat"),
        None,
    )
    handoff_data = {"handoff_status": "none", "requirements": None, "acceptance_criteria": None}
    if chat_member and chat_member.workspace_id:
        ctx = db.query(ProjectContext).filter(
            ProjectContext.project_id == project.id,
            ProjectContext.workspace_id == chat_member.workspace_id,
        ).first()
        if ctx:
            handoff_data = {
                "handoff_status": getattr(ctx, "handoff_status", "none") or "none",
                "requirements": getattr(ctx, "requirements", None),
                "acceptance_criteria": getattr(ctx, "acceptance_criteria", None),
                "handoff_at": ctx.handoff_at.isoformat() if getattr(ctx, "handoff_at", None) else None,
            }

    result = _project_dict(project, include_members=False)
    result["handoff"] = handoff_data
    return result


@router.patch("/{project_id}")
def update_project(
    project_id: int,
    req: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新项目信息。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return _project_dict(project, include_members=True)


@router.post("/{project_id}/members")
def add_member(
    project_id: int,
    req: MemberInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """为项目添加成员（仅负责人）。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    if len(project.members) >= MAX_MEMBERS:
        raise HTTPException(400, f"项目成员最多 {MAX_MEMBERS} 人")

    # 防重复
    if any(m.user_id == req.user_id for m in project.members):
        raise HTTPException(400, "该用户已是项目成员")

    member = ProjectMember(
        project_id=project_id,
        user_id=req.user_id,
        role_desc=req.role_desc,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return _member_dict(member)


@router.delete("/{project_id}/members/{member_id}")
def remove_member(
    project_id: int,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除项目成员（仅负责人）。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    member = db.get(ProjectMember, member_id)
    if not member or member.project_id != project_id:
        raise HTTPException(404, "成员不存在")

    db.delete(member)
    db.commit()
    return {"ok": True}


@router.post("/{project_id}/complete")
def complete_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """负责人完结项目。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    project.status = ProjectStatus.COMPLETED
    db.commit()
    return {"ok": True, "status": project.status.value}


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解散项目（归档所有 workspace）。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    # 归档关联 workspace
    for member in project.members:
        if member.workspace_id:
            ws = db.get(Workspace, member.workspace_id)
            if ws:
                from app.models.workspace import WorkspaceStatus
                ws.status = WorkspaceStatus.ARCHIVED

    project.status = ProjectStatus.ARCHIVED
    db.commit()
    return {"ok": True}


@router.get("/{project_id}/reports")
def list_reports(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取日/周报列表。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权查看该项目")

    reports = (
        db.query(ProjectReport)
        .filter(ProjectReport.project_id == project_id)
        .order_by(ProjectReport.created_at.desc())
        .all()
    )
    return [_report_dict(r) for r in reports]


@router.post("/{project_id}/reports/generate")
async def generate_report(
    project_id: int,
    report_type: str = "daily",  # query param
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发生成报告。"""
    project = _get_project_or_404(project_id, db)
    _require_owner(project, user)

    if report_type not in ("daily", "weekly"):
        raise HTTPException(400, "report_type 必须为 daily 或 weekly")

    from app.services.project_engine import project_engine
    content = await project_engine.generate_report(project, report_type, db)
    return {"ok": True, "content": content}


@router.get("/{project_id}/context")
def get_context(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取所有 workspace 的压缩上下文。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权查看该项目")

    contexts = db.query(ProjectContext).filter(
        ProjectContext.project_id == project_id
    ).all()
    return [_context_dict(c) for c in contexts]


@router.post("/{project_id}/context/sync")
async def sync_context(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发压缩上下文同步。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权操作该项目")

    from app.services.project_engine import project_engine
    await project_engine.sync_context(project, db)
    return {"ok": True}


@router.post("/{project_id}/knowledge-shares")
def create_knowledge_share(
    project_id: int,
    req: KnowledgeShareCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """成员共享知识到项目。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权操作该项目")

    # 防重复
    existing = db.query(ProjectKnowledgeShare).filter(
        ProjectKnowledgeShare.project_id == project_id,
        ProjectKnowledgeShare.knowledge_id == req.knowledge_id,
        ProjectKnowledgeShare.user_id == user.id,
    ).first()
    if existing:
        return {"ok": True, "message": "Already shared"}

    share = ProjectKnowledgeShare(
        project_id=project_id,
        user_id=user.id,
        knowledge_id=req.knowledge_id,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return {
        "ok": True,
        "id": share.id,
        "knowledge_id": share.knowledge_id,
        "shared_at": share.shared_at.isoformat(),
    }


@router.delete("/{project_id}/knowledge-shares/{share_id}")
def delete_knowledge_share(
    project_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消共享知识。"""
    share = db.get(ProjectKnowledgeShare, share_id)
    if not share or share.project_id != project_id:
        raise HTTPException(404, "共享记录不存在")
    if share.user_id != user.id and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权取消该共享")

    db.delete(share)
    db.commit()
    return {"ok": True}


# ─── 项目对话 ──────────────────────────────────────────────────────────────────

@router.get("/{project_id}/conversations")
def list_project_conversations(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回该项目所有成员的对话列表（群组视图）。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权访问该项目")

    from app.models.conversation import Conversation
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.is_active == True,
        )
        .order_by(Conversation.updated_at.desc())
        .all()
    )

    result = []
    for c in convs:
        owner = db.get(User, c.user_id) if c.user_id else None
        last_msg = c.messages[-1] if c.messages else None
        result.append({
            "id": c.id,
            "owner_id": c.user_id,
            "owner_name": owner.display_name if owner else None,
            "last_message": last_msg.content[:100] if last_msg else None,
            "updated_at": c.updated_at.isoformat(),
        })
    return result


@router.post("/{project_id}/knowledge/upload")
async def upload_project_knowledge(
    project_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    category: str = Form("experience"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传文件到项目知识库，自动写入 project_knowledge_shares。"""
    import os
    import uuid
    from app.config import settings
    from app.models.knowledge import KnowledgeEntry
    from app.services.knowledge_service import submit_knowledge
    from app.utils.file_parser import extract_text

    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权操作该项目")

    if not file:
        raise HTTPException(400, "请上传文件")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1]
    saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")

    with open(saved_path, "wb") as f:
        f.write(await file.read())

    try:
        content = extract_text(saved_path)
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    entry_title = title or file.filename or "项目文件"

    from app.services.review_policy import review_policy
    sensitive_flags = review_policy.detect_sensitive(content)
    strategic_flags = review_policy.detect_strategic(content)
    capture_mode = "upload" if (sensitive_flags or strategic_flags) else "upload_ai_clean"

    entry = KnowledgeEntry(
        title=entry_title,
        content=content,
        category=category,
        industry_tags=[],
        platform_tags=[],
        topic_tags=[],
        created_by=user.id,
        department_id=user.department_id,
        source_type="upload",
        source_file=file.filename,
        capture_mode=capture_mode,
    )
    db.add(entry)
    db.flush()

    try:
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        cls_result = await classify(content, db)
        if cls_result:
            apply_classification_to_entry(entry, cls_result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Auto-classification failed: {e}")

    entry = submit_knowledge(db, entry)

    # 自动关联到项目知识库
    share = ProjectKnowledgeShare(
        project_id=project_id,
        user_id=user.id,
        knowledge_id=entry.id,
    )
    db.add(share)
    db.commit()

    try:
        from app.services import vector_service
        vector_service.index_knowledge(entry.id, content, created_by=user.id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Vector indexing failed: {e}")

    return {
        "ok": True,
        "knowledge_id": entry.id,
        "title": entry_title,
        "status": entry.status.value,
        "content_length": len(content),
    }


class ExtractTasksRequest(BaseModel):
    conversation_id: int


@router.post("/{project_id}/extract-tasks")
async def extract_project_tasks(
    project_id: int,
    req: ExtractTasksRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从对话中 AI 提取任务/进度/bug，写入 tasks 表。"""
    project = _get_project_or_404(project_id, db)
    if not _is_member_or_owner(project, user):
        raise HTTPException(403, "无权操作该项目")

    from app.models.conversation import Conversation, Message
    conv = db.get(Conversation, req.conversation_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404, "对话不存在或不属于该项目")

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == req.conversation_id)
        .order_by(Message.created_at)
        .all()
    )
    if not msgs:
        return {"tasks": []}

    conversation_text = "\n".join(
        f"[{m.role.value}] {m.content[:300]}" for m in msgs[-30:]
    )

    extract_prompt = f"""从以下项目对话中提取任务、进度节点和 bug。

对话内容：
{conversation_text}

请返回 JSON 数组，每个元素包含：
- title: 任务标题（简短，不超过50字）
- description: 详细描述
- type: "task" | "bug" | "milestone"
- priority: "urgent_important" | "important" | "urgent" | "neither"

只返回 JSON 数组，不要其他内容。如果没有可提取的任务，返回空数组 []。"""

    from app.services.llm_gateway import llm_gateway
    import json
    try:
        result, _ = await llm_gateway.chat(
            model_config=llm_gateway.get_config(db),
            messages=[{"role": "user", "content": extract_prompt}],
            temperature=0.2,
            max_tokens=1000,
        )
        raw = result.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        items = json.loads(raw.strip())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Task extraction failed: {e}")
        return {"tasks": [], "error": str(e)}

    from app.models.task import Task, TaskPriority, TaskStatus
    created = []
    for item in items[:10]:  # 最多10条
        priority_val = item.get("priority", "neither")
        try:
            priority = TaskPriority(priority_val)
        except ValueError:
            priority = TaskPriority.NEITHER

        task = Task(
            title=str(item.get("title", ""))[:200],
            description=item.get("description", ""),
            priority=priority,
            status=TaskStatus.PENDING,
            assignee_id=user.id,
            created_by_id=user.id,
            source_type="ai_extracted",
            source_id=req.conversation_id,
            conversation_id=req.conversation_id,
            project_id=project_id,
            metadata_={"type": item.get("type", "task")},
        )
        db.add(task)
        db.flush()
        created.append({
            "id": task.id,
            "title": task.title,
            "type": item.get("type", "task"),
            "priority": priority.value,
        })

    db.commit()
    return {"tasks": created}
