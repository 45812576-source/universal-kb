"""项目模块 API 路由。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
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
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ─── Schemas ──────────────────────────────────────────────────────────────────

class MemberInput(BaseModel):
    user_id: int
    role_desc: str


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    department_id: Optional[int] = None
    members: list[MemberInput] = []


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
    """创建项目（负责人填写名称、背景、成员列表+分工描述）。"""
    if len(req.members) > MAX_MEMBERS:
        raise HTTPException(400, f"项目成员最多 {MAX_MEMBERS} 人")

    project = Project(
        name=req.name,
        description=req.description,
        owner_id=user.id,
        department_id=req.department_id or user.department_id,
        status=ProjectStatus.DRAFT,
    )
    db.add(project)
    db.flush()

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
    return [_project_dict(p) for p in all_projects]


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
    ]
    return result


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
