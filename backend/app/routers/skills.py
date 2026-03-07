from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User, Role
from app.models.skill import Skill, SkillVersion, SkillStatus

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hybrid"
    department_id: int = None
    knowledge_tags: list[str] = []
    auto_inject: bool = True
    system_prompt: str
    variables: list[str] = []
    model_config_id: int = None


class SkillVersionCreate(BaseModel):
    system_prompt: str
    variables: list[str] = []
    model_config_id: int = None
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
        "system_prompt_preview": (latest.system_prompt[:120] + "...") if latest and len(latest.system_prompt) > 120 else (latest.system_prompt if latest else ""),
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
    if status:
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
    return {
        **_skill_summary(skill),
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "system_prompt": v.system_prompt,
                "variables": v.variables or [],
                "model_config_id": v.model_config_id,
                "change_note": v.change_note,
                "created_by": v.created_by,
                "created_at": v.created_at.isoformat(),
            }
            for v in skill.versions
        ],
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
