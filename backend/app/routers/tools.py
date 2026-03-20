"""Tool registry CRUD API."""
from __future__ import annotations

import ast
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.tool import ToolRegistry, SkillTool, ToolType, UserSavedTool
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
    is_owner = user is not None and t.created_by == user.id
    d: dict = {
        "id": t.id,
        "name": t.name,
        "display_name": t.display_name,
        "description": t.description,
        "is_active": t.is_active,
        "scope": t.scope or "personal",
        "status": t.status or "draft",
        "department_id": t.department_id,
    }
    if is_admin or is_owner:
        d["tool_type"] = t.tool_type.value
        d["input_schema"] = t.input_schema
        d["output_format"] = t.output_format
        d["created_by"] = t.created_by
        d["created_at"] = t.created_at.isoformat() if t.created_at else None
    if is_super:
        d["config"] = t.config
    return d


@router.get("/my-saved")
def list_my_saved_tools(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(UserSavedTool).filter(UserSavedTool.user_id == user.id).all()
    result = []
    for row in rows:
        tool = db.get(ToolRegistry, row.tool_id)
        if not tool:
            continue
        d = _tool_dict(tool, user)
        d["saved_at"] = row.saved_at.isoformat() if row.saved_at else None
        result.append(d)
    return result


@router.post("/save-from-market")
def save_tool_from_market(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool_id = body.get("tool_id")
    if not tool_id:
        raise HTTPException(400, "tool_id required")
    tool = db.get(ToolRegistry, tool_id)
    if not tool or tool.status != "published":
        raise HTTPException(404, "工具不存在或未发布")
    existing = db.query(UserSavedTool).filter(
        UserSavedTool.user_id == user.id, UserSavedTool.tool_id == tool_id
    ).first()
    if existing:
        return {"ok": True}
    db.add(UserSavedTool(user_id=user.id, tool_id=tool_id))
    db.commit()
    return {"ok": True}


@router.delete("/save-from-market/{tool_id}")
def unsave_tool_from_market(
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.query(UserSavedTool).filter(
        UserSavedTool.user_id == user.id, UserSavedTool.tool_id == tool_id
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.get("")
def list_tools(
    mine: bool = False,
    scope: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(ToolRegistry)
    if mine:
        q = q.filter(ToolRegistry.created_by == user.id)
    elif scope == "department":
        q = q.filter(
            ToolRegistry.department_id == user.department_id,
            ToolRegistry.status == "published",
        )
    elif scope == "company":
        q = q.filter(ToolRegistry.scope == "company", ToolRegistry.status == "published")
    elif user.role == Role.EMPLOYEE:
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


@router.patch("/{tool_id}/status")
def update_tool_status(
    tool_id: int,
    status: str,
    scope: str = "personal",
    department_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(404, "Tool not found")
    if tool.created_by != user.id and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "无权操作")
    if status == "published":
        tool.status = "published"
        tool.scope = scope
        tool.department_id = department_id
        tool.is_active = True
    elif status == "archived":
        tool.status = "archived"
        tool.is_active = False
    elif status == "draft":
        tool.status = "draft"
        tool.scope = "personal"
    tool.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"ok": True, "status": tool.status, "scope": tool.scope}


@router.delete("/{tool_id}")
def delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if user.role != Role.SUPER_ADMIN and tool.created_by != user.id:
        raise HTTPException(status_code=403, detail="无权删除此工具")
    db.delete(tool)
    db.commit()
    return {"ok": True}


def _parse_py_tool(source: str) -> dict:
    """从 Python 源码中提取第一个函数的名称、docstring 和参数 schema。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise HTTPException(400, f"Python 语法错误：{e}")

    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if not funcs:
        raise HTTPException(400, "文件中未找到函数定义")

    func = funcs[0]
    name = func.name
    docstring = ast.get_docstring(func) or ""
    description = docstring.split("\n")[0].strip() if docstring else ""

    properties: dict = {}
    for arg in func.args.args:
        if arg.arg == "self":
            continue
        arg_type = "string"
        if arg.annotation:
            ann = ast.unparse(arg.annotation) if hasattr(ast, "unparse") else ""
            if ann in ("int", "float"):
                arg_type = "number"
            elif ann == "bool":
                arg_type = "boolean"
        properties[arg.arg] = {"type": arg_type}

    input_schema = {"type": "object", "properties": properties} if properties else {}

    return {"name": name, "display_name": name, "description": description, "input_schema": input_schema}


@router.post("/upload-py")
async def upload_tool_py(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传 .py 文件创建/更新工具。"""
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "只支持 .py 文件")

    raw = await file.read()
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码必须是 UTF-8")

    parsed = _parse_py_tool(source)
    func_name = parsed["name"]

    existing = (
        db.query(ToolRegistry)
        .filter(ToolRegistry.name == func_name, ToolRegistry.created_by == user.id)
        .first()
    )

    if existing:
        existing.description = parsed["description"] or existing.description
        existing.input_schema = parsed["input_schema"]
        existing.config = {**(existing.config or {}), "source": source}
        existing.updated_at = datetime.datetime.utcnow()
        db.commit()
        return {"action": "updated", "id": existing.id, "name": func_name}
    else:
        tool = ToolRegistry(
            name=func_name,
            display_name=parsed["display_name"],
            description=parsed["description"],
            tool_type=ToolType.BUILTIN,
            config={"source": source},
            input_schema=parsed["input_schema"],
            output_format="json",
            is_active=False,
            scope="personal",
            status="draft",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)
        return {"action": "created", "id": tool.id, "name": func_name}


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

@router.get("/tool-bindings/{tool_id}")
def get_tool_skill_bindings(
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.skill import Skill
    rows = (
        db.query(Skill)
        .join(SkillTool, SkillTool.skill_id == Skill.id)
        .filter(SkillTool.tool_id == tool_id)
        .all()
    )
    return [{"id": s.id, "name": s.name} for s in rows]


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
