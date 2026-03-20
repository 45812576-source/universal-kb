"""Tool registry CRUD API."""
from __future__ import annotations

import ast
import datetime
from pathlib import Path
from typing import Optional

_TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _write_tool_module(func_name: str, source: str) -> None:
    """把源码写到 app/tools/<func_name>.py，让 importlib 能加载。"""
    _TOOLS_DIR.mkdir(exist_ok=True)
    init = _TOOLS_DIR / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    (_TOOLS_DIR / f"{func_name}.py").write_text(source, encoding="utf-8")

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.tool import ToolRegistry, SkillTool, ToolType, UserSavedTool
from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
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


class ToolStatusUpdate(BaseModel):
    status: str
    scope: str = "personal"
    department_id: Optional[int] = None
    deploy_info: Optional[dict] = None  # 提交发布时附带的部署说明


@router.patch("/{tool_id}/status")
def update_tool_status(
    tool_id: int,
    body: ToolStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    status = body.status
    scope = body.scope
    department_id = body.department_id
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(404, "Tool not found")
    if tool.created_by != user.id and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "无权操作")

    if status == "published":
        # 超管直接发布
        if user.role == Role.SUPER_ADMIN:
            tool.status = "published"
            tool.scope = scope
            tool.department_id = department_id
            tool.is_active = True
            db.commit()
            return {"id": tool_id, "status": "published", "scope": scope}

        # 部门管理员 → 进入审核，等超管审批（super_pending）
        if user.role == Role.DEPT_ADMIN:
            tool.status = "reviewing"
            tool.scope = scope
            tool.department_id = department_id
            tool.updated_at = datetime.datetime.utcnow()
            existing = db.query(ApprovalRequest).filter(
                ApprovalRequest.request_type == ApprovalRequestType.TOOL_PUBLISH,
                ApprovalRequest.target_id == tool_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            ).first()
            if not existing:
                db.add(ApprovalRequest(
                    request_type=ApprovalRequestType.TOOL_PUBLISH,
                    target_id=tool_id,
                    target_type="tool",
                    requester_id=user.id,
                    stage="super_pending",
                ))
            db.commit()
            return {"id": tool_id, "status": "reviewing", "stage": "super_pending"}

        # 普通员工 → 进入审核，先等部门管理员审批（dept_pending）
        tool.status = "reviewing"
        tool.scope = scope
        tool.department_id = department_id
        tool.updated_at = datetime.datetime.utcnow()
        existing = db.query(ApprovalRequest).filter(
            ApprovalRequest.request_type == ApprovalRequestType.TOOL_PUBLISH,
            ApprovalRequest.target_id == tool_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        ).first()
        if not existing:
            db.add(ApprovalRequest(
                request_type=ApprovalRequestType.TOOL_PUBLISH,
                target_id=tool_id,
                target_type="tool",
                requester_id=user.id,
                stage="dept_pending",
            ))
        db.commit()
        return {"id": tool_id, "status": "reviewing", "stage": "dept_pending"}

    elif status == "archived":
        tool.status = "archived"
        tool.is_active = False
    elif status == "draft":
        tool.status = "draft"
        tool.scope = "personal"
        tool.is_active = False

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


def _parse_manifest_comments(source: str) -> dict:
    """从源码注释中解析 __le_desk_manifest__ 块。

    支持格式：
        # __le_desk_manifest__
        # invocation_mode: registered_table | file_upload | chat
        # data_sources:
        #   - key: table_name, type: registered_table, required: true, description: 要运算的业务表
        #   - key: file_id, type: uploaded_file, accept: .xlsx .csv, required: false
        # permissions: read:hr_employees, write:hr_bonus
        # preconditions:
        #   - table 必须包含字段 employee_id, base_salary
    """
    lines = source.splitlines()

    # 找到 manifest 块的起始行
    start = None
    for i, line in enumerate(lines):
        if "__le_desk_manifest__" in line:
            start = i + 1
            break
    if start is None:
        return {}

    # 收集连续的注释行（直到空行或非注释行）
    block_lines = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            block_lines.append(stripped[1:].lstrip())  # 去掉 # 和前导空格
        elif stripped == "":
            continue
        else:
            break  # 遇到代码行，停止

    if not block_lines:
        return {}

    manifest: dict = {}
    current_list_key: str | None = None

    for line in block_lines:
        if not line:
            current_list_key = None
            continue

        # 顶层 key: value
        if ":" in line and not line.startswith("-"):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                # 逗号分隔的列表值
                if "," in val:
                    manifest[key] = [v.strip() for v in val.split(",") if v.strip()]
                else:
                    manifest[key] = val
                current_list_key = None
            else:
                # 值为空 → 下面是列表
                manifest[key] = []
                current_list_key = key

        # 列表项
        elif line.startswith("-") and current_list_key:
            item_str = line[1:].strip()
            # data_sources 条目：key: xxx, type: yyy, ...
            if "," in item_str and ":" in item_str:
                item: dict = {}
                for part in item_str.split(","):
                    part = part.strip()
                    if ":" in part:
                        k, _, v = part.partition(":")
                        k, v = k.strip(), v.strip()
                        # 布尔值
                        if v.lower() == "true":
                            v = True
                        elif v.lower() == "false":
                            v = False
                        # accept 字段转列表
                        if k == "accept" and isinstance(v, str):
                            v = [x.strip() for x in v.split() if x.strip()]
                        item[k] = v
                manifest[current_list_key].append(item)
            else:
                manifest[current_list_key].append(item_str)

    return manifest


def _parse_py_tool(source: str) -> dict:
    """从 Python 源码中提取第一个函数的名称、docstring、参数 schema 和 manifest。"""
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
    required_args: list = []
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

    # 有默认值的参数数量，用于判断必填
    n_defaults = len(func.args.defaults)
    n_args = len([a for a in func.args.args if a.arg != "self"])
    n_required = n_args - n_defaults
    for i, arg in enumerate([a for a in func.args.args if a.arg != "self"]):
        if i < n_required:
            required_args.append(arg.arg)

    input_schema: dict = {}
    if properties:
        input_schema = {"type": "object", "properties": properties}
        if required_args:
            input_schema["required"] = required_args

    manifest = _parse_manifest_comments(source)

    return {
        "name": name,
        "display_name": name,
        "description": description,
        "input_schema": input_schema,
        "manifest": manifest,
    }


_VALID_INVOCATION_MODES = {"chat", "registered_table", "file_upload"}
_VALID_SOURCE_TYPES = {"registered_table", "uploaded_file", "chat_context"}


def _validate_manifest(manifest: dict) -> list[str]:
    """对 manifest 做合理性检查，返回警告列表（不阻断上传）。"""
    if not manifest:
        return []

    warnings: list[str] = []

    mode = manifest.get("invocation_mode")
    if mode and mode not in _VALID_INVOCATION_MODES:
        warnings.append(f"invocation_mode '{mode}' 未知，合法值：{sorted(_VALID_INVOCATION_MODES)}")

    for ds in manifest.get("data_sources", []):
        ds_type = ds.get("type")
        if ds_type and ds_type not in _VALID_SOURCE_TYPES:
            warnings.append(f"data_sources[{ds.get('key')}].type '{ds_type}' 未知，合法值：{sorted(_VALID_SOURCE_TYPES)}")
        if not ds.get("key"):
            warnings.append("data_sources 条目缺少 key 字段")

    return warnings


class McpConfigRequest(BaseModel):
    description: str


@router.post("/generate-mcp-config")
async def generate_mcp_config(
    body: McpConfigRequest,
    user: User = Depends(get_current_user),
):
    """用自然语言描述生成 MCP 工具的 manifest 配置。"""
    from app.services.llm_gateway import llm_gateway
    import json as _json

    prompt = f"""你是企业内部工具配置专家。根据用户对 MCP 工具的描述，生成一份结构化配置。

用户描述：{body.description}

请返回如下 JSON（只返回 JSON，不要解释）：
{{
  "display_name": "工具中文显示名",
  "description": "一句话功能描述",
  "invocation_mode": "chat",
  "data_sources": [
    {{"key": "参数名", "type": "chat_context|registered_table|uploaded_file", "required": true, "description": "说明"}}
  ],
  "permissions": ["read:表名或资源"],
  "preconditions": ["运行前提条件"],
  "env_requirements": "需要的环境变量或外部依赖说明"
}}

规则：
- invocation_mode 只能是 chat / registered_table / file_upload 之一
- data_sources 只列出工具真正需要的输入参数，不需要输入则为空数组
- permissions 格式为 read:资源名 或 write:资源名
- 如无特殊前提条件或权限，对应字段返回空数组或空字符串"""

    try:
        model_config = llm_gateway.get_lite_config()
        # 生成配置需要更多 token
        model_config = {**model_config, "max_tokens": 1024}
        content, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        # 提取 JSON 块
        text = content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        config = _json.loads(text.strip())
    except _json.JSONDecodeError as e:
        raise HTTPException(500, f"AI 返回格式解析失败：{e}")
    except Exception as e:
        raise HTTPException(500, f"生成失败：{e}")

    return config


@router.post("/upload-mcp")
async def upload_mcp_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传 MCP 服务 zip 包，解压并分析项目类型，创建 draft 工具记录。"""
    import tempfile
    import os as _os
    from app.services.mcp_installer import analyze_zip, extract_zip

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "只支持 .zip 文件")

    # 保存上传的 zip
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        analysis = analyze_zip(tmp_path)
    except zipfile.BadZipFile:
        _os.unlink(tmp_path)
        raise HTTPException(400, "不是有效的 zip 文件")
    except Exception as e:
        _os.unlink(tmp_path)
        raise HTTPException(400, f"解析失败：{e}")

    # 用文件名（去掉 .zip）作为工具名基础
    base_name = file.filename[:-4].lower().replace(" ", "_").replace("-", "_")
    # 避免重名
    tool_name = base_name
    suffix = 1
    while db.query(ToolRegistry).filter(ToolRegistry.name == tool_name, ToolRegistry.created_by == user.id).first():
        tool_name = f"{base_name}_{suffix}"
        suffix += 1

    # 解压到安装目录
    try:
        install_dir = extract_zip(tmp_path, tool_name)
    except Exception as e:
        _os.unlink(tmp_path)
        raise HTTPException(500, f"解压失败：{e}")
    finally:
        _os.unlink(tmp_path)

    # 创建 draft 工具记录
    existing = db.query(ToolRegistry).filter(
        ToolRegistry.name == tool_name, ToolRegistry.created_by == user.id
    ).first()

    config = {
        "install_dir": str(install_dir),
        "project_type": analysis["project_type"],
        "run_cmd": analysis["run_cmd"],
    }

    if existing:
        existing.config = config
        existing.updated_at = datetime.datetime.utcnow()
        db.commit()
        tool_id = existing.id
        action = "updated"
    else:
        tool = ToolRegistry(
            name=tool_name,
            display_name=tool_name,
            description="",
            tool_type=ToolType.MCP,
            config=config,
            input_schema={},
            output_format="json",
            is_active=False,
            scope="personal",
            status="draft",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)
        tool_id = tool.id
        action = "created"

    return {
        "action": action,
        "id": tool_id,
        "name": tool_name,
        "project_type": analysis["project_type"],
        "run_cmd": analysis["run_cmd"],
        "warnings": analysis["warnings"],
    }


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
    manifest = parsed.get("manifest", {})

    # 写到磁盘，importlib 才能加载
    try:
        _write_tool_module(func_name, source)
    except OSError as e:
        raise HTTPException(500, f"写入工具模块失败：{e}")

    existing = (
        db.query(ToolRegistry)
        .filter(ToolRegistry.name == func_name, ToolRegistry.created_by == user.id)
        .first()
    )

    if existing:
        existing.description = parsed["description"] or existing.description
        existing.input_schema = parsed["input_schema"]
        existing.config = {**(existing.config or {}), "source": source, "manifest": manifest}
        existing.updated_at = datetime.datetime.utcnow()
        db.commit()
        return {
            "action": "updated",
            "id": existing.id,
            "name": func_name,
            "manifest": manifest,
            "manifest_warnings": _validate_manifest(manifest),
        }
    else:
        tool = ToolRegistry(
            name=func_name,
            display_name=parsed["display_name"],
            description=parsed["description"],
            tool_type=ToolType.BUILTIN,
            config={"source": source, "manifest": manifest},
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
        return {
            "action": "created",
            "id": tool.id,
            "name": func_name,
            "manifest": manifest,
            "manifest_warnings": _validate_manifest(manifest),
        }


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
