"""Tool registry CRUD API."""
from __future__ import annotations

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.tool import ToolRegistry, SkillTool, ToolType
from app.models.user import Role, User
from app.services.tool_executor import tool_executor

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = None
    tool_type: ToolType
    config: Optional[dict] = None
    input_schema: Optional[dict] = None
    output_format: str = "json"


class ToolUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    tool_type: Optional[ToolType] = None
    config: Optional[dict] = None
    input_schema: Optional[dict] = None
    output_format: Optional[str] = None
    is_active: Optional[bool] = None


class ToolTestRequest(BaseModel):
    params: dict = {}


def _tool_dict(t: ToolRegistry, user: User | None = None) -> dict:
    is_super = user is not None and user.role == Role.SUPER_ADMIN
    is_admin = user is not None and user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    d: dict = {
        "id": t.id,
        "name": t.name,
        "display_name": t.display_name,
        "description": t.description,
        "is_active": t.is_active,
    }
    if is_admin:
        d["tool_type"] = t.tool_type.value
        d["input_schema"] = t.input_schema
        d["output_format"] = t.output_format
        d["created_by"] = t.created_by
        d["created_at"] = t.created_at.isoformat() if t.created_at else None
    if is_super:
        d["config"] = t.config
    return d


@router.get("")
def list_tools(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(ToolRegistry)
    if user.role == Role.EMPLOYEE:
        q = q.filter(ToolRegistry.is_active == True)
    tools = q.order_by(ToolRegistry.created_at.desc()).all()
    return [_tool_dict(t, user) for t in tools]


@router.post("")
def create_tool(
    body: ToolCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    if db.query(ToolRegistry).filter(ToolRegistry.name == body.name).first():
        raise HTTPException(status_code=400, detail="Tool name already exists")
    tool = ToolRegistry(
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        tool_type=body.tool_type,
        config=body.config or {},
        input_schema=body.input_schema or {},
        output_format=body.output_format,
        created_by=user.id,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return _tool_dict(tool, user)


@router.get("/{tool_id}")
def get_tool(tool_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return _tool_dict(tool, user)


@router.put("/{tool_id}")
def update_tool(
    tool_id: int,
    body: ToolUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(tool, field, value)
    tool.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(tool)
    return _tool_dict(tool, user)


@router.delete("/{tool_id}")
def delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    db.delete(tool)
    db.commit()
    return {"ok": True}


@router.post("/{tool_id}/test")
async def test_tool(
    tool_id: int,
    body: ToolTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    result = await tool_executor.execute_tool(db, tool.name, body.params, user.id)
    return result


# --- Skill <-> Tool binding ---

@router.get("/skill/{skill_id}/tools")
def get_skill_tools(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tools = tool_executor.get_tools_for_skill(db, skill_id)
    return [_tool_dict(t, user) for t in tools]


@router.post("/skill/{skill_id}/tools/{tool_id}")
def bind_skill_tool(
    skill_id: int,
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    existing = db.query(SkillTool).filter(
        SkillTool.skill_id == skill_id,
        SkillTool.tool_id == tool_id,
    ).first()
    if existing:
        return {"ok": True, "message": "Already bound"}
    link = SkillTool(skill_id=skill_id, tool_id=tool_id)
    db.add(link)
    db.commit()
    return {"ok": True}


@router.delete("/skill/{skill_id}/tools/{tool_id}")
def unbind_skill_tool(
    skill_id: int,
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    row = db.query(SkillTool).filter(
        SkillTool.skill_id == skill_id,
        SkillTool.tool_id == tool_id,
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}
