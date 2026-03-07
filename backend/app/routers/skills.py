from fastapi import APIRouter, Depends, HTTPException, Query
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


class SkillVersionCreate(BaseModel):
    system_prompt: str
    variables: list[str] = []
    model_config_id: Optional[int] = None
    change_note: str = ""


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
    }


@router.get("")
def list_skills(
    status: str = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Skill)
    # employee can only see published skills
    if user.role == Role.EMPLOYEE:
        q = q.filter(Skill.status == SkillStatus.PUBLISHED)
    elif status:
        q = q.filter(Skill.status == status)
    return [_skill_summary(s) for s in q.order_by(Skill.updated_at.desc()).all()]


@router.post("")
def create_skill(
    req: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    if db.query(Skill).filter(Skill.name == req.name).first():
        raise HTTPException(400, f"Skill '{req.name}' already exists")

    skill = Skill(
        name=req.name,
        description=req.description,
        mode=req.mode,
        department_id=req.department_id,
        knowledge_tags=req.knowledge_tags,
        auto_inject=req.auto_inject,
        created_by=user.id,
    )
    db.add(skill)
    db.flush()

    v = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=req.system_prompt,
        variables=req.variables,
        model_config_id=req.model_config_id,
        created_by=user.id,
        change_note="初始版本",
    )
    db.add(v)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}


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
        created_by=user.id,
        change_note=req.change_note,
    )
    db.add(v)
    db.commit()
    return {"version": v.version, "id": v.id}


@router.patch("/{skill_id}/status")
def update_status(
    skill_id: int,
    status: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if status not in [s.value for s in SkillStatus]:
        raise HTTPException(400, f"Invalid status: {status}")
    skill.status = status
    db.commit()
    return {"id": skill_id, "status": status}


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
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
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
        v = SkillVersion(
            skill_id=skill_id,
            version=max_ver + 1,
            system_prompt=new_prompt,
            variables=[],
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
