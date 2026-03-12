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
    model_config_id: Optional[int] = None
    output_schema: Optional[dict] = None


class SkillVersionCreate(BaseModel):
    system_prompt: str
    variables: list[str] = []
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
    # 员工无权创建 Skill
    if user.role == Role.EMPLOYEE:
        raise HTTPException(403, "员工不能创建 Skill")

    # DEPT_ADMIN 限额：最多 3 个未发布 Skill
    if user.role == Role.DEPT_ADMIN:
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

    if db.query(Skill).filter(Skill.name == req.name).first():
        raise HTTPException(400, f"Skill '{req.name}' already exists")

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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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

    existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()

    if existing:
        # Add a new version
        latest = existing.versions[0] if existing.versions else None
        new_ver = (latest.version + 1) if latest else 1
        v = SkillVersion(
            skill_id=existing.id,
            version=new_ver,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
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
        # Create new skill, auto-publish
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


@router.get("/{skill_id}")
def get_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    # employee: no versions at all
    if user.role == Role.EMPLOYEE:
        return _skill_summary(skill)

    # dept_admin: show prompt only for own department's skills
    is_own_dept = (user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id)
    is_super = user.role == Role.SUPER_ADMIN

    def _version_dict(v) -> dict:
        base = {
            "id": v.id,
            "version": v.version,
            "variables": v.variables or [],
            "model_config_id": v.model_config_id,
            "output_schema": v.output_schema,
            "change_note": v.change_note,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat(),
        }
        if is_super or is_own_dept:
            base["system_prompt"] = v.system_prompt
        return base

    return {
        **_skill_summary(skill),
        "versions": [_version_dict(v) for v in skill.versions],
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
    skill.status = status
    if scope is not None:
        skill.scope = scope
    if department_id is not None:
        skill.department_id = department_id

    # 发布时自动生成 SkillPolicy（幂等）
    if status == SkillStatus.PUBLISHED.value:
        _ensure_skill_policy(skill_id, user, db)

    db.commit()
    return {"id": skill_id, "status": status, "scope": skill.scope}


def _ensure_skill_policy(skill_id: int, user: User, db) -> None:
    """发布时自动生成 SkillPolicy（若已存在则跳过）。
    默认 publish_scope 按 skill.scope 映射：
      personal → self_only, department → same_role, company → org_wide
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

    policy = SkillPolicy(
        skill_id=skill_id,
        publish_scope=publish_scope,
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
    db.delete(skill)
    db.commit()
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

