import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User, Role
from app.models.skill import Skill, SkillVersion, SkillStatus
from app.services.llm_gateway import llm_gateway

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hybrid"
    department_id: Optional[int] = None
    knowledge_tags: list[str] = []
    auto_inject: bool = True
    system_prompt: str
    variables: list[str] = []
    required_inputs: list[dict] = []
    model_config_id: Optional[int] = None
    output_schema: Optional[dict] = None


class SkillVersionCreate(BaseModel):
    system_prompt: str
    variables: list[str] = []
    required_inputs: list[dict] = []
    model_config_id: Optional[int] = None
    change_note: str = ""
    output_schema: Optional[dict] = None


def _skill_summary(s: Skill) -> dict:
    latest = s.versions[0] if s.versions else None
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "mode": s.mode.value,
        "status": s.status.value,
        "knowledge_tags": s.knowledge_tags or [],
        "auto_inject": s.auto_inject,
        "current_version": latest.version if latest else 0,
        "department_id": s.department_id,
        "created_at": s.created_at.isoformat(),
        "scope": s.scope or "personal",
        "created_by": s.created_by,
        "source_type": s.source_type or "local",
        "source_files": s.source_files or [],
    }


class SaveFromMarketRequest(BaseModel):
    skill_id: int


@router.post("/save-from-market")
def save_from_market(
    req: SaveFromMarketRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存公司级 Skill 到个人收藏。"""
    from app.models.skill import UserSavedSkill
    from sqlalchemy.exc import IntegrityError

    skill = db.get(Skill, req.skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    saved = UserSavedSkill(user_id=user.id, skill_id=req.skill_id)
    db.add(saved)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # 已保存，忽略
    return {"ok": True}


@router.delete("/save-from-market/{skill_id}")
def unsave_from_market(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消保存。"""
    from app.models.skill import UserSavedSkill

    row = (
        db.query(UserSavedSkill)
        .filter(UserSavedSkill.user_id == user.id, UserSavedSkill.skill_id == skill_id)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.get("/my-saved")
def list_my_saved(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户保存的公司级 Skill 列表。"""
    from app.models.skill import UserSavedSkill
    import datetime as _dt

    rows = (
        db.query(UserSavedSkill)
        .filter(UserSavedSkill.user_id == user.id)
        .all()
    )

    result = []
    for row in rows:
        skill = db.get(Skill, row.skill_id)
        if not skill:
            continue
        summary = _skill_summary(skill)
        # has_update: skill.updated_at > saved_at
        has_update = False
        if skill.updated_at and row.saved_at:
            has_update = skill.updated_at > row.saved_at
        summary["has_update"] = has_update
        summary["saved_at"] = row.saved_at.isoformat() if row.saved_at else None
        result.append(summary)
    return result


@router.get("")
def list_skills(
    status: str = None,
    scope: str = None,
    mine: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import or_
    q = db.query(Skill)
    # 员工：看已发布的 + 自己创建的（含草稿）
    if user.role == Role.EMPLOYEE:
        q = q.filter(
            or_(
                Skill.status == SkillStatus.PUBLISHED,
                Skill.created_by == user.id,
            )
        )
    elif status:
        q = q.filter(Skill.status == status)

    if mine:
        q = q.filter(Skill.created_by == user.id)
    elif scope == "department":
        q = q.filter(Skill.department_id == user.department_id, Skill.status == SkillStatus.PUBLISHED)
    elif scope == "company":
        q = q.filter(Skill.scope == "company", Skill.status == SkillStatus.PUBLISHED)

    return [_skill_summary(s) for s in q.order_by(Skill.updated_at.desc()).all()]


MAX_EMPLOYEE_UNPUBLISHED_SKILLS = 3


@router.post("")
def create_skill(
    req: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 员工/部门管理员：最多 3 个未发布的个人 Skill
    if user.role in (Role.EMPLOYEE, Role.DEPT_ADMIN):
        unpublished_count = (
            db.query(Skill)
            .filter(
                Skill.created_by == user.id,
                Skill.status != SkillStatus.PUBLISHED,
            )
            .count()
        )
        if unpublished_count >= MAX_EMPLOYEE_UNPUBLISHED_SKILLS:
            raise HTTPException(
                400,
                f"最多只能有 {MAX_EMPLOYEE_UNPUBLISHED_SKILLS} 个未发布 Skill，请先发布或删除已有草稿",
            )

    if db.query(Skill).filter(Skill.name == req.name, Skill.created_by == user.id).first():
        raise HTTPException(400, f"你已有同名 Skill '{req.name}'，请修改名称或更新已有版本")

    # 员工创建的 Skill 固定为个人草稿，不可自行设置 scope
    skill = Skill(
        name=req.name,
        description=req.description,
        mode=req.mode,
        scope="personal" if user.role == Role.EMPLOYEE else (req.department_id and "department" or "company"),
        department_id=None if user.role == Role.EMPLOYEE else req.department_id,
        knowledge_tags=req.knowledge_tags,
        auto_inject=req.auto_inject,
        created_by=user.id,
        status=SkillStatus.DRAFT,
    )
    db.add(skill)
    db.flush()

    v = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=req.system_prompt,
        variables=req.variables,
        required_inputs=req.required_inputs,
        model_config_id=req.model_config_id,
        output_schema=req.output_schema,
        created_by=user.id,
        change_note="初始版本",
    )
    db.add(v)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}


def _parse_skill_md(content: str) -> dict:
    """Parse a SKILL.md file: YAML frontmatter (name, description) + body as system_prompt.

    Also extracts {variable} placeholders from the body.
    """
    frontmatter = {}
    body = content

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                frontmatter[key.strip()] = val.strip()
        body = content[fm_match.end():]

    name = frontmatter.get("name", "").strip()
    description = frontmatter.get("description", "").strip()

    # Extract {variable} placeholders (but not {{escaped}} or {code blocks})
    variables = sorted(set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", body)))

    return {
        "name": name,
        "description": description,
        "system_prompt": body.strip(),
        "variables": [f"{{{v}}}" for v in variables],
    }


@router.post("/upload-md")
async def upload_skill_md(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a .md file to create or update a Skill.

    If a skill with the same name exists, a new version is created.
    Otherwise a new skill is created and auto-published.
    """
    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(400, "只支持 .md 文件")

    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码必须是 UTF-8")

    parsed = _parse_skill_md(content)
    if not parsed["name"]:
        raise HTTPException(400, "文件缺少 frontmatter 中的 name 字段")

    # 先查自己名下的同名 Skill；超管可以更新任意同名 Skill
    if user.role == Role.SUPER_ADMIN:
        existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()
    else:
        existing = db.query(Skill).filter(
            Skill.name == parsed["name"],
            Skill.created_by == user.id,
        ).first()
        # 名字被别人占用时给出明确提示
        if not existing:
            name_taken = db.query(Skill).filter(Skill.name == parsed["name"]).first()
            if name_taken:
                raise HTTPException(400, f"Skill 名称「{parsed['name']}」已被占用，请修改 md 文件中的 name 字段后重新上传")

    if existing:
        # Add a new version
        latest = existing.versions[0] if existing.versions else None
        new_ver = (latest.version + 1) if latest else 1
        v = SkillVersion(
            skill_id=existing.id,
            version=new_ver,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            required_inputs=latest.required_inputs if latest else [],
            model_config_id=latest.model_config_id if latest else None,
            output_schema=latest.output_schema if latest else None,
            created_by=user.id,
            change_note=f"从 md 文件上传更新 v{new_ver}",
        )
        db.add(v)
        if parsed["description"]:
            existing.description = parsed["description"]
        db.commit()
        return {
            "action": "updated",
            "id": existing.id,
            "name": existing.name,
            "version": new_ver,
        }
    else:
        # 超管直接发布；其他角色先存为草稿，由用户自行决定何时提交审批
        if user.role == Role.SUPER_ADMIN:
            new_status = SkillStatus.PUBLISHED
            new_scope = "company"
        else:
            new_status = SkillStatus.DRAFT
            new_scope = "personal"
        skill = Skill(
            name=parsed["name"],
            description=parsed["description"],
            mode="hybrid",
            status=new_status,
            scope=new_scope,
            auto_inject=True,
            created_by=user.id,
        )
        db.add(skill)
        db.flush()
        v = SkillVersion(
            skill_id=skill.id,
            version=1,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            created_by=user.id,
            change_note="从 md 文件上传创建",
        )
        db.add(v)
        db.commit()
        db.refresh(skill)
        return {
            "action": "created",
            "id": skill.id,
            "name": skill.name,
            "version": 1,
            "status": new_status.value,
        }


@router.post("/batch-upload-md")
async def batch_upload_skill_md(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Upload multiple .md files at once."""
    results = []
    for f in files:
        if not f.filename or not f.filename.endswith(".md"):
            results.append({"filename": f.filename, "error": "不是 .md 文件"})
            continue
        raw = await f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            results.append({"filename": f.filename, "error": "编码错误"})
            continue
        parsed = _parse_skill_md(content)
        if not parsed["name"]:
            results.append({"filename": f.filename, "error": "缺少 name"})
            continue

        existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()
        if existing:
            latest = existing.versions[0] if existing.versions else None
            new_ver = (latest.version + 1) if latest else 1
            v = SkillVersion(
                skill_id=existing.id,
                version=new_ver,
                system_prompt=parsed["system_prompt"],
                variables=parsed["variables"],
                required_inputs=latest.required_inputs if latest else [],
                model_config_id=latest.model_config_id if latest else None,
                output_schema=latest.output_schema if latest else None,
                created_by=user.id,
                change_note=f"批量上传更新 v{new_ver}",
            )
            db.add(v)
            if parsed["description"]:
                existing.description = parsed["description"]
            results.append({"filename": f.filename, "action": "updated", "id": existing.id, "name": existing.name, "version": new_ver})
        else:
            skill = Skill(
                name=parsed["name"],
                description=parsed["description"],
                mode="hybrid",
                status=SkillStatus.PUBLISHED,
                scope="company",
                auto_inject=True,
                created_by=user.id,
            )
            db.add(skill)
            db.flush()
            v = SkillVersion(
                skill_id=skill.id,
                version=1,
                system_prompt=parsed["system_prompt"],
                variables=parsed["variables"],
                created_by=user.id,
                change_note="批量上传创建",
            )
            db.add(v)
            results.append({"filename": f.filename, "action": "created", "id": skill.id, "name": parsed["name"], "version": 1})

    db.commit()
    return {"results": results, "total": len(results)}


@router.post("/upload-zip")
async def upload_skill_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传 .zip 压缩包创建复杂 Skill（主 .md 文件 + 附属参考文件/脚本）。

    zip 包结构：
    - 必须包含一个 .md 文件（优先 index.md / README.md，否则取第一个 .md）
    - 其余文件作为附属文件存储到 uploads/skills/<skill_id>/ 目录
    """
    import zipfile as _zipfile
    import tempfile
    import os as _os
    import shutil
    from pathlib import Path as _Path
    from app.config import settings

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "只支持 .zip 文件")

    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        with _zipfile.ZipFile(tmp_path, "r") as zf:
            names = zf.namelist()
    except _zipfile.BadZipFile:
        _os.unlink(tmp_path)
        raise HTTPException(400, "不是有效的 zip 文件")

    # 找主 md 文件（忽略 __MACOSX 等系统目录）
    md_files = [n for n in names if n.endswith(".md") and not n.startswith("__") and "/" not in n.lstrip("/")]
    # 也允许一层子目录内的 md
    if not md_files:
        md_files = [n for n in names if n.endswith(".md") and not n.startswith("__")]
    if not md_files:
        _os.unlink(tmp_path)
        raise HTTPException(400, "zip 包中未找到 .md 文件")

    # 优先选 index.md / README.md
    main_md = next(
        (n for n in md_files if _Path(n).name.lower() in ("index.md", "readme.md", "skill.md")),
        md_files[0],
    )

    with _zipfile.ZipFile(tmp_path, "r") as zf:
        md_content = zf.read(main_md).decode("utf-8")
        # 其他文件（排除系统文件和其他 .md 可选保留）
        other_files = [
            n for n in names
            if n != main_md
            and not n.endswith("/")
            and not _Path(n).name.startswith(".")
            and not n.startswith("__MACOSX")
        ]

    parsed = _parse_skill_md(md_content)
    if not parsed["name"]:
        _os.unlink(tmp_path)
        raise HTTPException(400, "主 .md 文件缺少 frontmatter 中的 name 字段")

    # 检查同名 skill
    existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()

    if user.role == Role.SUPER_ADMIN:
        new_status = SkillStatus.PUBLISHED
        new_scope = "company"
        initial_stage = None
    else:
        new_status = SkillStatus.REVIEWING
        new_scope = "personal"
        initial_stage = "super_pending" if user.role == Role.DEPT_ADMIN else "dept_pending"

    if existing:
        skill_id = existing.id
        latest = existing.versions[0] if existing.versions else None
        new_ver = (latest.version + 1) if latest else 1
        v = SkillVersion(
            skill_id=skill_id,
            version=new_ver,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            required_inputs=latest.required_inputs if latest else [],
            model_config_id=latest.model_config_id if latest else None,
            created_by=user.id,
            change_note="从 zip 包上传更新",
        )
        db.add(v)
        if parsed["description"]:
            existing.description = parsed["description"]
        action = "updated"
        version = new_ver
    else:
        skill = Skill(
            name=parsed["name"],
            description=parsed["description"],
            mode="hybrid",
            status=new_status,
            scope=new_scope,
            auto_inject=True,
            created_by=user.id,
            source_type="local",
        )
        db.add(skill)
        db.flush()
        skill_id = skill.id
        v = SkillVersion(
            skill_id=skill_id,
            version=1,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            created_by=user.id,
            change_note="从 zip 包上传创建",
        )
        db.add(v)
        if initial_stage:
            from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus as AStatus
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.SKILL_PUBLISH,
                target_id=skill_id,
                target_type="skill",
                requester_id=user.id,
                status=AStatus.PENDING,
                stage=initial_stage,
            )
            db.add(approval)
        action = "created"
        version = 1

    # 提取附属文件到 uploads/skills/<skill_id>/
    upload_base = _Path(settings.UPLOAD_DIR) / "skills" / str(skill_id)
    upload_base.mkdir(parents=True, exist_ok=True)

    saved_files = []
    if other_files:
        with _zipfile.ZipFile(tmp_path, "r") as zf:
            for name in other_files:
                safe_name = _Path(name).name  # 只取文件名，防止路径穿越
                if not safe_name:
                    continue
                dest = upload_base / safe_name
                data = zf.read(name)
                dest.write_bytes(data)
                saved_files.append({
                    "filename": safe_name,
                    "path": f"uploads/skills/{skill_id}/{safe_name}",
                    "size": len(data),
                })

    # 更新 source_files
    target_skill = db.get(Skill, skill_id)
    if target_skill:
        target_skill.source_files = saved_files

    db.commit()
    _os.unlink(tmp_path)

    return {
        "action": action,
        "id": skill_id,
        "name": parsed["name"],
        "version": version,
        "status": new_status.value if not existing else (existing.status.value),
        "source_files": saved_files,
        "stage": initial_stage,
    }


# ─── Skill file CRUD ──────────────────────────────────────────────────────────

TEXT_EXTENSIONS = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".toml", ".xml", ".csv"}


def _check_skill_write_access(skill: Skill, user: User):
    if user.role != Role.SUPER_ADMIN and skill.created_by != user.id:
        raise HTTPException(403, "无权操作此 Skill 的文件")


def _safe_skill_dir(skill_id: int) -> "Path":
    from pathlib import Path
    from app.config import settings
    return Path(settings.UPLOAD_DIR) / "skills" / str(skill_id)


def _safe_file_path(skill_id: int, filename: str) -> "Path":
    from pathlib import Path
    base = _safe_skill_dir(skill_id)
    # 防止路径穿越：只取文件名部分
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        raise HTTPException(400, "无效文件名")
    return base / safe


@router.get("/{skill_id}/files/{filename}")
def get_skill_file(
    skill_id: int,
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取 skill 附属文件内容（文本）。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    ext = Path(filename).suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        raise HTTPException(400, "该文件类型不支持文本预览")

    return {"content": path.read_text(encoding="utf-8", errors="replace")}


class SkillFileUpdate(BaseModel):
    content: str


@router.put("/{skill_id}/files/{filename}")
def update_skill_file(
    skill_id: int,
    filename: str,
    req: SkillFileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存 skill 附属文件内容。若文件不存在则创建并追加到 source_files。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding="utf-8")

    size = len(req.content.encode("utf-8"))
    rel_path = f"uploads/skills/{skill_id}/{path.name}"
    files = list(skill.source_files or [])
    for f in files:
        if f.get("filename") == path.name:
            f["size"] = size
            break
    else:
        files.append({"filename": path.name, "path": rel_path, "size": size})
    skill.source_files = files
    db.commit()
    return {"ok": True, "filename": path.name, "size": size}


@router.post("/{skill_id}/files")
async def upload_skill_file(
    skill_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传新 asset 文件到 skill 目录。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    if not file.filename:
        raise HTTPException(400, "文件名不能为空")
    safe_name = Path(file.filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "无效文件名")

    dest = _safe_skill_dir(skill_id) / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    dest.write_bytes(data)

    files = list(skill.source_files or [])
    rel_path = f"uploads/skills/{skill_id}/{safe_name}"
    for f in files:
        if f.get("filename") == safe_name:
            f["size"] = len(data)
            break
    else:
        files.append({"filename": safe_name, "path": rel_path, "size": len(data)})
    skill.source_files = files
    db.commit()
    return {"ok": True, "filename": safe_name, "size": len(data), "source_files": files}


@router.delete("/{skill_id}/files/{filename}")
def delete_skill_file(
    skill_id: int,
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除 skill 附属文件（磁盘 + source_files 列表）。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    if path.exists():
        path.unlink()

    files = [f for f in (skill.source_files or []) if f.get("filename") != path.name]
    skill.source_files = files
    db.commit()
    return {"ok": True, "source_files": files}


# ─── Skill ranking / hot list ─────────────────────────────────────────────────

@router.get("/ranking")
def get_skill_ranking(
    scope: str = "company",
    department_id: Optional[int] = None,
    days: int = 30,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Skill 热榜：按过去 N 天的对话数排序。
    scope=company → 通用榜（company scope skills）
    scope=department → 专业榜（department scope skills，可按 department_id 筛选）
    """
    import datetime as dt
    from sqlalchemy import func
    from app.models.conversation import Conversation

    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    q = db.query(Skill).filter(
        Skill.status == SkillStatus.PUBLISHED,
        Skill.scope == scope,
    )
    if scope == "department" and department_id:
        q = q.filter(Skill.department_id == department_id)
    candidate_skill_ids = [s.id for s in q.all()]

    if not candidate_skill_ids:
        return []

    counts = (
        db.query(
            Conversation.skill_id,
            func.count(Conversation.id).label("conv_count"),
            func.count(func.distinct(Conversation.user_id)).label("user_count"),
        )
        .filter(
            Conversation.skill_id.in_(candidate_skill_ids),
            Conversation.created_at >= since,
        )
        .group_by(Conversation.skill_id)
        .order_by(func.count(Conversation.id).desc())
        .limit(limit)
        .all()
    )

    result = []
    for i, (skill_id, conv_count, user_count) in enumerate(counts):
        s = db.get(Skill, skill_id)
        if not s:
            continue
        total = (
            db.query(func.count(Conversation.id))
            .filter(Conversation.skill_id == skill_id)
            .scalar() or 0
        )
        result.append({
            "rank": i + 1,
            "skill_id": skill_id,
            "name": s.name,
            "description": s.description,
            "scope": s.scope,
            "department_id": s.department_id,
            "knowledge_tags": s.knowledge_tags or [],
            "current_version": max((v.version for v in s.versions), default=0),
            "conv_count_recent": conv_count,
            "user_count_recent": user_count,
            "conv_count_total": total,
        })

    return result


# ─── Usage stats (super_admin only) ──────────────────────────────────────────

@router.get("/{skill_id}/usage")
def get_skill_usage(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管查看某个 Skill 的使用详情：每个用户使用了多少次对话、多少条消息。"""
    from sqlalchemy import func
    from app.models.conversation import Conversation, Message, MessageRole

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    from app.models.user import User as UserModel
    stats = (
        db.query(
            Conversation.user_id,
            func.count(Conversation.id).label("conv_count"),
        )
        .filter(Conversation.skill_id == skill_id)
        .group_by(Conversation.user_id)
        .all()
    )

    result = []
    total_convs = 0
    for user_id, conv_count in stats:
        u = db.get(UserModel, user_id)
        msg_count = (
            db.query(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .filter(
                Conversation.skill_id == skill_id,
                Conversation.user_id == user_id,
                Message.role == MessageRole.USER,
            )
            .scalar() or 0
        )
        total_convs += conv_count
        result.append({
            "user_id": user_id,
            "display_name": u.display_name if u else f"user#{user_id}",
            "department_id": u.department_id if u else None,
            "conv_count": conv_count,
            "msg_count": msg_count,
        })

    result.sort(key=lambda x: x["conv_count"], reverse=True)
    return {
        "skill_id": skill_id,
        "skill_name": skill.name,
        "total_conv_count": total_convs,
        "total_user_count": len(result),
        "by_user": result,
    }


def _get_rejection_comment(skill_id: int, db: Session) -> str | None:
    """返回该 Skill 最近一次被驳回时审批人写的意见（供提交人看）。"""
    try:
        from app.models.permission import ApprovalRequest, ApprovalAction, ApprovalStatus, ApprovalActionType
        req = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.target_id == skill_id,
                ApprovalRequest.target_type == "skill",
                ApprovalRequest.status == ApprovalStatus.REJECTED,
            )
            .order_by(ApprovalRequest.created_at.desc())
            .first()
        )
        if not req:
            return None
        action = (
            db.query(ApprovalAction)
            .filter(
                ApprovalAction.request_id == req.id,
                ApprovalAction.action == ApprovalActionType.REJECT,
            )
            .order_by(ApprovalAction.created_at.desc())
            .first()
        )
        return action.comment if action and action.comment else None
    except Exception:
        return None


@router.get("/{skill_id}")
def get_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    is_external = skill.source_type in ("imported", "forked")
    is_own_dept = (user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id)
    is_super = user.role == Role.SUPER_ADMIN

    # employee: 外部引入的可看完整内容，内部 local 只看摘要
    if user.role == Role.EMPLOYEE:
        if not is_external:
            return _skill_summary(skill)
        # 外部引入：返回最新版 system_prompt（只读，不含版本历史）
        latest = skill.versions[0] if skill.versions else None
        return {
            **_skill_summary(skill),
            "source_type": skill.source_type,
            "system_prompt": latest.system_prompt if latest else "",
        }

    def _version_dict(v) -> dict:
        base = {
            "id": v.id,
            "version": v.version,
            "variables": v.variables or [],
            "required_inputs": v.required_inputs or [],
            "model_config_id": v.model_config_id,
            "output_schema": v.output_schema,
            "change_note": v.change_note,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat(),
        }
        if is_super or is_own_dept or is_external:
            base["system_prompt"] = v.system_prompt
        return base

    return {
        **_skill_summary(skill),
        "source_type": skill.source_type,
        "versions": [_version_dict(v) for v in skill.versions],
        "rejection_comment": _get_rejection_comment(skill.id, db),
    }


@router.put("/{skill_id}")
def update_skill(
    skill_id: int,
    req: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    skill.name = req.name
    skill.description = req.description
    skill.mode = req.mode
    skill.department_id = req.department_id
    skill.knowledge_tags = req.knowledge_tags
    skill.auto_inject = req.auto_inject
    db.commit()
    return {"id": skill.id}


@router.post("/{skill_id}/versions")
def add_version(
    skill_id: int,
    req: SkillVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    max_ver = max((v.version for v in skill.versions), default=0)
    v = SkillVersion(
        skill_id=skill_id,
        version=max_ver + 1,
        system_prompt=req.system_prompt,
        variables=req.variables,
        required_inputs=req.required_inputs,
        model_config_id=req.model_config_id,
        output_schema=req.output_schema,
        created_by=user.id,
        change_note=req.change_note,
    )
    # Mark as customized if this is an imported/forked skill being modified
    if skill.source_type in ("imported", "forked"):
        import datetime as _dt
        skill.is_customized = True
        skill.local_modified_at = _dt.datetime.utcnow()
    db.add(v)
    db.commit()
    return {"version": v.version, "id": v.id}


@router.patch("/{skill_id}/status")
def update_status(
    skill_id: int,
    status: str = Query(...),
    scope: Optional[str] = Query(None),          # company / department / personal
    department_id: Optional[int] = Query(None),  # 指定部门时填写
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """更新 Skill 状态，发布时可同时设置可见范围。
    scope: company=全公司, department=指定部门（需提供 department_id）, personal=仅自己
    """
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if status not in [s.value for s in SkillStatus]:
        raise HTTPException(400, f"Invalid status: {status}")

    # DEPT_ADMIN 申请发布 → 转为审核中，创建审批单等超管审批
    if status == SkillStatus.PUBLISHED.value and user.role == Role.DEPT_ADMIN:
        if scope is not None:
            skill.scope = scope
        if department_id is not None:
            skill.department_id = department_id
        skill.status = SkillStatus.REVIEWING
        db.flush()
        # 幂等：同一 skill 不重复创建待审批单
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        existing_approval = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.target_id == skill_id,
                ApprovalRequest.target_type == "skill",
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .first()
        )
        if not existing_approval:
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.SKILL_PUBLISH,
                target_id=skill_id,
                target_type="skill",
                requester_id=user.id,
                status=ApprovalStatus.PENDING,
            )
            db.add(approval)
        db.commit()
        return {"id": skill_id, "status": SkillStatus.REVIEWING.value, "scope": skill.scope}

    skill.status = status
    if scope is not None:
        skill.scope = scope
    if department_id is not None:
        skill.department_id = department_id

    # SUPER_ADMIN 直接发布时生成 SkillPolicy
    if status == SkillStatus.PUBLISHED.value:
        _ensure_skill_policy(skill_id, user, db)

    db.commit()
    return {"id": skill_id, "status": status, "scope": skill.scope}


class BatchPublishRequest(BaseModel):
    skill_ids: list[int]
    scope: str = "company"  # company / department / personal


@router.post("/batch-publish")
def batch_publish(
    req: BatchPublishRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管批量发布 Skill，全部设为 published + 指定 scope。"""
    if req.scope not in ("company", "department", "personal"):
        raise HTTPException(400, "Invalid scope")
    results = []
    for skill_id in req.skill_ids:
        skill = db.get(Skill, skill_id)
        if not skill:
            results.append({"id": skill_id, "ok": False, "reason": "not found"})
            continue
        skill.status = SkillStatus.PUBLISHED
        skill.scope = req.scope
        _ensure_skill_policy(skill_id, user, db)
        results.append({"id": skill_id, "ok": True, "name": skill.name})
    db.commit()
    ok_count = sum(1 for r in results if r["ok"])
    return {"published": ok_count, "total": len(req.skill_ids), "results": results}


def _ensure_skill_policy(skill_id: int, user: User, db) -> None:
    """发布时自动生成 SkillPolicy（若已存在则跳过）。
    publish_scope（可用范围）按 skill.scope 映射：personal→self_only, department→same_role, company→org_wide
    view_scope（可见范围）默认比 publish_scope 宽一级（至少同部门可见）
    """
    from app.models.permission import SkillPolicy, PublishScope
    existing = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
    if existing:
        return

    skill = db.get(Skill, skill_id)
    scope_map = {
        "personal": PublishScope.SELF_ONLY,
        "department": PublishScope.SAME_ROLE,
        "company": PublishScope.ORG_WIDE,
    }
    publish_scope = scope_map.get(skill.scope or "personal", PublishScope.SAME_ROLE)
    # view_scope 默认比 use_scope 宽一级
    view_scope_map = {
        PublishScope.SELF_ONLY: PublishScope.SAME_ROLE,
        PublishScope.SAME_ROLE: PublishScope.SAME_ROLE,
        PublishScope.CROSS_ROLE: PublishScope.ORG_WIDE,
        PublishScope.ORG_WIDE: PublishScope.ORG_WIDE,
    }
    view_scope = view_scope_map.get(publish_scope, PublishScope.ORG_WIDE)

    policy = SkillPolicy(
        skill_id=skill_id,
        publish_scope=publish_scope,
        view_scope=view_scope,
        default_data_scope={},
    )
    db.add(policy)
    db.flush()


class AIEditRequest(BaseModel):
    instruction: str
    model_config_id: Optional[int] = None


class AIEditApply(BaseModel):
    proposed: dict
    change_note: str


@router.post("/{skill_id}/edit-with-ai")
async def edit_with_ai(
    skill_id: int,
    req: AIEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Generate AI-powered edit preview from natural language instruction."""
    from app.services.skill_editor import skill_editor
    model_config = llm_gateway.get_config(db, req.model_config_id)
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    try:
        preview = await skill_editor.edit_skill(skill_id, req.instruction, model_config, db)
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{skill_id}/edit-with-ai/apply")
def apply_ai_edit(
    skill_id: int,
    req: AIEditApply,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Apply AI-generated edit by creating a new version."""
    from app.services.skill_editor import skill_editor
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    try:
        result = skill_editor.apply_edit(skill_id, req.proposed, req.change_note, user.id, db)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


class IterateRequest(BaseModel):
    suggestion_ids: list[int]
    model_config_id: Optional[int] = None


class IterateApply(BaseModel):
    proposed: dict
    change_note: str
    suggestion_ids: list[int]


@router.post("/{skill_id}/iterate")
async def iterate_from_suggestions(
    skill_id: int,
    req: IterateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Generate AI-powered diff based on adopted suggestions."""
    from app.services.skill_editor import skill_editor
    model_config = llm_gateway.get_config(db, req.model_config_id)
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    try:
        preview = await skill_editor.iterate_from_suggestions(
            skill_id, req.suggestion_ids, model_config, db
        )
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{skill_id}/iterate/apply")
def apply_iterate(
    skill_id: int,
    req: IterateApply,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Apply iterated version and generate attributions."""
    from app.services.skill_editor import skill_editor
    from app.services.attribution import attribution_service
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    latest = skill.versions[0] if skill.versions else None
    version_from = latest.version if latest else 0
    try:
        result = skill_editor.apply_edit(skill_id, req.proposed, req.change_note, user.id, db)
        version_to = result["version"]
        # Fire-and-forget attribution (non-blocking)
        import asyncio
        try:
            model_config = llm_gateway.get_config(db)
            asyncio.create_task(
                attribution_service.generate_attributions(
                    skill_id=skill_id,
                    version_from=version_from,
                    version_to=version_to,
                    suggestion_ids=req.suggestion_ids,
                    model_config=model_config,
                    db=db,
                )
            )
        except Exception:
            pass  # Attribution is best-effort
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role == Role.EMPLOYEE:
        # 员工只能删自己创建的未发布 Skill（释放名额）
        if skill.created_by != user.id:
            raise HTTPException(403, "只能删除自己创建的 Skill")
        if skill.status == SkillStatus.PUBLISHED:
            raise HTTPException(403, "已发布的 Skill 不可删除，请联系管理员")
    elif user.role == Role.DEPT_ADMIN:
        if skill.department_id != user.department_id and skill.created_by != user.id:
            raise HTTPException(403, "只能删除本部门的 Skill")

    # 级联删除关联记录（外键约束）
    from sqlalchemy import text
    sid = skill_id
    # 子 skill 的 parent 引用置空（自引用外键）
    db.execute(text("UPDATE skills SET parent_skill_id = NULL WHERE parent_skill_id = :sid"), {"sid": sid})
    # skill_policies 下的子表先删
    db.execute(text("DELETE FROM skill_mask_overrides WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM role_policy_overrides WHERE skill_policy_id IN (SELECT id FROM skill_policies WHERE skill_id = :sid)"), {"sid": sid})
    db.execute(text("DELETE FROM skill_output_schemas WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_agent_connections WHERE skill_policy_id IN (SELECT id FROM skill_policies WHERE skill_id = :sid) OR connected_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_executions WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_schema_caches WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_templates WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_policies WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM approval_actions WHERE request_id IN (SELECT id FROM approval_requests WHERE target_id = :sid AND target_type = 'skill')"), {"sid": sid})
    db.execute(text("DELETE FROM approval_requests WHERE target_id = :sid AND target_type = 'skill'"), {"sid": sid})
    db.execute(text("DELETE FROM workspace_skills WHERE skill_id = :sid"), {"sid": sid})
    # skill 自身关联表
    db.execute(text("DELETE FROM skill_attributions WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_tools WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_upstream_checks WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_data_queries WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM user_saved_skills WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_suggestions WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_versions WHERE skill_id = :sid"), {"sid": sid})
    # conversations 置空 skill_id（保留对话记录）
    db.execute(text("UPDATE conversations SET skill_id = NULL WHERE skill_id = :sid"), {"sid": sid})
    # 清理附属文件目录（如果有）
    from pathlib import Path as _Path
    from app.config import settings
    import shutil as _shutil
    skill_files_dir = _Path(settings.UPLOAD_DIR) / "skills" / str(sid)
    if skill_files_dir.exists():
        _shutil.rmtree(skill_files_dir, ignore_errors=True)

    try:
        db.delete(skill)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"删除失败，数据库约束错误：{e}")
    return {"ok": True}


@router.get("/{skill_id}/upstream-diff")
def get_upstream_diff(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Return upstream vs local diff for an imported skill."""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if not skill.upstream_content:
        return {"has_upstream": False}

    latest = skill.versions[0] if skill.versions else None
    local_prompt = latest.system_prompt if latest else ""

    from app.models.mcp import SkillUpstreamCheck
    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    return {
        "has_upstream": True,
        "source_type": skill.source_type,
        "upstream_version": skill.upstream_version,
        "upstream_synced_at": skill.upstream_synced_at.isoformat() if skill.upstream_synced_at else None,
        "is_customized": skill.is_customized,
        "upstream_content": skill.upstream_content,
        "local_content": local_prompt,
        "has_new_upstream": latest_check.has_diff if latest_check else False,
        "new_upstream_version": latest_check.upstream_version if latest_check else None,
        "diff_summary": latest_check.diff_summary if latest_check else None,
        "check_action": latest_check.action if latest_check else None,
    }


class UpstreamSyncRequest(BaseModel):
    action: str  # overwrite / ignore


@router.post("/{skill_id}/upstream-sync")
def upstream_sync(
    skill_id: int,
    req: UpstreamSyncRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Handle sync decision: overwrite local with upstream, or ignore upstream update."""
    from app.models.mcp import SkillUpstreamCheck, McpSource
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id, SkillUpstreamCheck.has_diff == True)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    if req.action == "ignore":
        if latest_check:
            latest_check.action = "ignored"
        db.commit()
        return {"ok": True, "action": "ignored"}

    if req.action == "overwrite":
        source = db.query(McpSource).filter(McpSource.is_active == True).first()
        if not source or not skill.upstream_id:
            raise HTTPException(400, "Cannot fetch upstream: no active source")

        from app.services.mcp_client import fetch_remote_skill, McpClientError
        try:
            remote = fetch_remote_skill(source, skill.upstream_id)
        except McpClientError as e:
            raise HTTPException(502, str(e))

        new_prompt = remote.get("system_prompt", "")
        new_version = remote.get("upstream_version", "")

        max_ver = max((v.version for v in skill.versions), default=0)
        import datetime as dt
        latest_local = skill.versions[0] if skill.versions else None
        v = SkillVersion(
            skill_id=skill_id,
            version=max_ver + 1,
            system_prompt=new_prompt,
            variables=[],
            output_schema=remote.get("output_schema", latest_local.output_schema if latest_local else None),
            created_by=user.id,
            change_note=f"同步上游 v{new_version}",
        )
        db.add(v)

        skill.upstream_content = new_prompt
        skill.upstream_version = new_version
        skill.upstream_synced_at = dt.datetime.utcnow()
        skill.is_customized = False

        if latest_check:
            latest_check.action = "synced"

        db.commit()
        return {"ok": True, "action": "overwrite", "new_version": v.version}

    raise HTTPException(400, f"Unknown action: {req.action}")


# ─── Usage stats (super_admin only) ──────────────────────────────────────────

