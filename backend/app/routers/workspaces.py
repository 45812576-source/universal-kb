"""Workspace CRUD, review, and skill/tool binding API."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillStatus
from app.models.tool import ToolRegistry
from app.models.user import Role, User
from app.models.skill import ModelConfig
from app.models.workspace import (
    Workspace,
    WorkspaceDataTable,
    WorkspaceSkill,
    WorkspaceStatus,
    WorkspaceTool,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

MAX_EMPLOYEE_DRAFT = 3


# ─── Serialisation ────────────────────────────────────────────────────────────

def _ws_dict(ws: Workspace, user: User, include_system_context: bool = False) -> dict:
    skills = [
        {
            "id": wsk.skill_id,
            "name": wsk.skill.name if wsk.skill else None,
            "description": wsk.skill.description if wsk.skill else None,
            "scope": wsk.skill.scope if wsk.skill else None,
        }
        for wsk in ws.workspace_skills
    ]
    tools = [
        {
            "id": wt.tool_id,
            "name": wt.tool.name if wt.tool else None,
            "display_name": wt.tool.display_name if wt.tool else None,
            "description": wt.tool.description if wt.tool else None,
            "tool_type": wt.tool.tool_type.value if wt.tool else None,
        }
        for wt in ws.workspace_tools
    ]
    data_tables = [wdt.table_name for wdt in ws.workspace_data_tables]

    d = {
        "id": ws.id,
        "name": ws.name,
        "description": ws.description,
        "icon": ws.icon,
        "color": ws.color,
        "category": ws.category,
        "status": ws.status.value,
        "created_by": ws.created_by,
        "department_id": ws.department_id,
        "visibility": ws.visibility,
        "welcome_message": ws.welcome_message,
        "sort_order": ws.sort_order,
        "skills": skills,
        "tools": tools,
        "data_tables": data_tables,
        "model_config_id": ws.model_config_id,
        "workspace_type": ws.workspace_type,
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
        "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
    }
    if include_system_context:
        d["system_context"] = ws.system_context
    return d


def _ws_summary(ws: Workspace) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "description": ws.description,
        "icon": ws.icon,
        "color": ws.color,
        "category": ws.category,
        "status": ws.status.value,
        "created_by": ws.created_by,
        "department_id": ws.department_id,
        "visibility": ws.visibility,
        "welcome_message": ws.welcome_message,
        "sort_order": ws.sort_order,
        "workspace_type": ws.workspace_type or "chat",
    }


# ─── Schemas ──────────────────────────────────────────────────────────────────

class WorkspaceCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "chat"
    color: str = "#00D1FF"
    category: str = "通用"
    visibility: str = "all"
    welcome_message: str = "你好，有什么可以帮你的？"
    system_context: Optional[str] = None
    model_config_id: Optional[int] = None
    department_id: Optional[int] = None
    sort_order: int = 0
    workspace_type: str = "chat"  # chat | opencode


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    category: Optional[str] = None
    visibility: Optional[str] = None
    welcome_message: Optional[str] = None
    system_context: Optional[str] = None
    model_config_id: Optional[int] = None
    department_id: Optional[int] = None
    sort_order: Optional[int] = None
    workspace_type: Optional[str] = None  # chat | opencode，仅 super_admin


class ReviewRequest(BaseModel):
    action: str  # "approve" | "reject"


# ─── Helper: employee draft count ─────────────────────────────────────────────

def _employee_draft_count(db: Session, user_id: int) -> int:
    return (
        db.query(Workspace)
        .filter(
            Workspace.created_by == user_id,
            Workspace.status == WorkspaceStatus.DRAFT,
            Workspace.is_active == True,
        )
        .count()
    )


# ─── Visibility helper ────────────────────────────────────────────────────────

def _user_can_see(ws: Workspace, user: User) -> bool:
    """Can user see this workspace?"""
    if ws.status == WorkspaceStatus.PUBLISHED and ws.is_active:
        if ws.visibility == "all":
            return True
        if ws.visibility == "department" and ws.department_id == user.department_id:
            return True
    # own draft / reviewing
    if ws.created_by == user.id:
        return True
    # admin can see everything
    if user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        return True
    return False


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def list_workspaces(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    all_ws = db.query(Workspace).filter(Workspace.is_active == True).order_by(
        Workspace.sort_order, Workspace.created_at
    ).all()
    result = [_ws_summary(ws) for ws in all_ws if _user_can_see(ws, user)]
    return result


@router.post("")
def create_workspace(
    req: WorkspaceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)

    # Employee quota check
    if not is_admin:
        if _employee_draft_count(db, user.id) >= MAX_EMPLOYEE_DRAFT:
            raise HTTPException(
                400,
                f"最多只能创建 {MAX_EMPLOYEE_DRAFT} 个草稿工作台，请先提交审核或删除已有草稿"
            )

    # Determine initial status
    if user.role == Role.SUPER_ADMIN:
        status = WorkspaceStatus.PUBLISHED
    elif user.role == Role.DEPT_ADMIN:
        status = WorkspaceStatus.PUBLISHED
    else:
        status = WorkspaceStatus.DRAFT

    # Only super_admin can set system_context and workspace_type
    system_context = req.system_context if user.role == Role.SUPER_ADMIN else None
    workspace_type = req.workspace_type if user.role == Role.SUPER_ADMIN else "chat"

    # Validate model_config_id if provided
    if req.model_config_id and not db.get(ModelConfig, req.model_config_id):
        raise HTTPException(400, "指定的模型配置不存在")

    ws = Workspace(
        name=req.name,
        description=req.description,
        icon=req.icon,
        color=req.color,
        category=req.category,
        visibility=req.visibility,
        welcome_message=req.welcome_message,
        system_context=system_context,
        model_config_id=req.model_config_id,
        department_id=req.department_id or user.department_id,
        sort_order=req.sort_order,
        status=status,
        created_by=user.id,
        workspace_type=workspace_type,
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return _ws_summary(ws)


@router.get("/{ws_id}")
def get_workspace(
    ws_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws or not _user_can_see(ws, user):
        raise HTTPException(404, "Workspace not found")
    include_ctx = user.role == Role.SUPER_ADMIN
    return _ws_dict(ws, user, include_system_context=include_ctx)


@router.put("/{ws_id}")
def update_workspace(
    ws_id: int,
    req: WorkspaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    is_super = user.role == Role.SUPER_ADMIN
    is_dept_admin = user.role == Role.DEPT_ADMIN
    is_own_draft = ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT

    # Permission check
    if not is_super:
        if is_dept_admin:
            if ws.status == WorkspaceStatus.PUBLISHED and ws.department_id != user.department_id:
                raise HTTPException(403, "只能编辑本部门的工作台")
        elif not is_own_draft:
            raise HTTPException(403, "只能编辑自己的草稿工作台")

    for field, value in req.model_dump(exclude_none=True).items():
        # Only super_admin can edit system_context, model_config_id, workspace_type
        if field in ("system_context", "model_config_id", "workspace_type") and not is_super:
            continue
        if field == "model_config_id" and value and not db.get(ModelConfig, value):
            raise HTTPException(400, "指定的模型配置不存在")
        setattr(ws, field, value)

    db.commit()
    db.refresh(ws)
    include_ctx = is_super
    return _ws_dict(ws, user, include_system_context=include_ctx)


@router.delete("/{ws_id}")
def delete_workspace(
    ws_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    if ws.workspace_type == "opencode":
        raise HTTPException(403, "系统工作台不可删除")

    is_super = user.role == Role.SUPER_ADMIN
    is_dept_admin = user.role == Role.DEPT_ADMIN
    is_own_draft = ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT

    if not is_super:
        if is_dept_admin:
            # dept_admin can delete draft workspaces in their department
            if not (ws.status == WorkspaceStatus.DRAFT and ws.department_id == user.department_id):
                raise HTTPException(403, "无权删除该工作台")
        elif not is_own_draft:
            raise HTTPException(403, "只能删除自己的草稿工作台")

    ws.is_active = False
    db.commit()
    return {"ok": True}


@router.patch("/{ws_id}/submit")
def submit_workspace(
    ws_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Employee submits draft for review."""
    ws = db.get(Workspace, ws_id)
    if not ws or ws.created_by != user.id:
        raise HTTPException(404, "Workspace not found")
    if ws.status != WorkspaceStatus.DRAFT:
        raise HTTPException(400, "只有草稿状态的工作台可以提交审核")

    ws.status = WorkspaceStatus.REVIEWING
    db.commit()
    return {"ok": True, "status": ws.status.value}


@router.patch("/{ws_id}/review")
def review_workspace(
    ws_id: int,
    req: ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Admin approves or rejects a reviewing workspace."""
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if ws.status != WorkspaceStatus.REVIEWING:
        raise HTTPException(400, "只有审核中的工作台可以审核")

    # dept_admin can only review workspaces in their department
    if user.role == Role.DEPT_ADMIN and ws.department_id != user.department_id:
        raise HTTPException(403, "只能审核本部门的工作台")

    if req.action == "approve":
        ws.status = WorkspaceStatus.PUBLISHED
    elif req.action == "reject":
        ws.status = WorkspaceStatus.DRAFT
    else:
        raise HTTPException(400, "action 必须为 approve 或 reject")

    db.commit()
    return {"ok": True, "status": ws.status.value}


# ─── Skill binding ────────────────────────────────────────────────────────────

@router.post("/{ws_id}/skills/{skill_id}")
def bind_skill(
    ws_id: int,
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws or not _user_can_see(ws, user):
        raise HTTPException(404, "Workspace not found")

    # Permission: admin unrestricted; employee only own draft + published skills
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin:
        if not (ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT):
            raise HTTPException(403, "无权绑定该工作台的 Skill")
        skill = db.get(Skill, skill_id)
        if not skill or skill.status != SkillStatus.PUBLISHED:
            raise HTTPException(400, "只能绑定已发布的 Skill")

    existing = db.query(WorkspaceSkill).filter(
        WorkspaceSkill.workspace_id == ws_id,
        WorkspaceSkill.skill_id == skill_id,
    ).first()
    if existing:
        return {"ok": True, "message": "Already bound"}

    db.add(WorkspaceSkill(workspace_id=ws_id, skill_id=skill_id))
    db.commit()
    return {"ok": True}


@router.delete("/{ws_id}/skills/{skill_id}")
def unbind_skill(
    ws_id: int,
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws or not _user_can_see(ws, user):
        raise HTTPException(404, "Workspace not found")

    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin and not (ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT):
        raise HTTPException(403, "无权操作该工作台")

    row = db.query(WorkspaceSkill).filter(
        WorkspaceSkill.workspace_id == ws_id,
        WorkspaceSkill.skill_id == skill_id,
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


# ─── Tool binding ─────────────────────────────────────────────────────────────

@router.post("/{ws_id}/tools/{tool_id}")
def bind_tool(
    ws_id: int,
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws or not _user_can_see(ws, user):
        raise HTTPException(404, "Workspace not found")

    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin:
        if not (ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT):
            raise HTTPException(403, "无权绑定该工作台的工具")
        tool = db.get(ToolRegistry, tool_id)
        if not tool or not tool.is_active:
            raise HTTPException(400, "只能绑定已启用的工具")

    existing = db.query(WorkspaceTool).filter(
        WorkspaceTool.workspace_id == ws_id,
        WorkspaceTool.tool_id == tool_id,
    ).first()
    if existing:
        return {"ok": True, "message": "Already bound"}

    db.add(WorkspaceTool(workspace_id=ws_id, tool_id=tool_id))
    db.commit()
    return {"ok": True}


@router.delete("/{ws_id}/tools/{tool_id}")
def unbind_tool(
    ws_id: int,
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, ws_id)
    if not ws or not _user_can_see(ws, user):
        raise HTTPException(404, "Workspace not found")

    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin and not (ws.created_by == user.id and ws.status == WorkspaceStatus.DRAFT):
        raise HTTPException(403, "无权操作该工作台")

    row = db.query(WorkspaceTool).filter(
        WorkspaceTool.workspace_id == ws_id,
        WorkspaceTool.tool_id == tool_id,
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


# ─── Batch binding ────────────────────────────────────────────────────────────

class BatchBindRequest(BaseModel):
    ids: list[int]


@router.put("/{ws_id}/skills")
def batch_set_skills(
    ws_id: int,
    req: BatchBindRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Replace all skill bindings for a workspace (admin only)."""
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == ws_id).delete()
    for skill_id in req.ids:
        if db.get(Skill, skill_id):
            db.add(WorkspaceSkill(workspace_id=ws_id, skill_id=skill_id))
    db.commit()
    db.refresh(ws)
    return _ws_dict(ws, user, include_system_context=True)


@router.put("/{ws_id}/tools")
def batch_set_tools(
    ws_id: int,
    req: BatchBindRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Replace all tool bindings for a workspace (admin only)."""
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    db.query(WorkspaceTool).filter(WorkspaceTool.workspace_id == ws_id).delete()
    for tool_id in req.ids:
        if db.get(ToolRegistry, tool_id):
            db.add(WorkspaceTool(workspace_id=ws_id, tool_id=tool_id))
    db.commit()
    db.refresh(ws)
    return _ws_dict(ws, user, include_system_context=True)
