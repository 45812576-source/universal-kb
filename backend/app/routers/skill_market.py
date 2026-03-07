"""Skill Market: browse external sources, import skills, manage MCP sources."""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.mcp import McpSource
from app.models.skill import Skill, SkillVersion, SkillStatus
from app.models.user import User, Role
from app.services.mcp_client import list_remote_skills, fetch_remote_skill, McpClientError

router = APIRouter(prefix="/api/skill-market", tags=["skill-market"])


class McpSourceCreate(BaseModel):
    name: str
    url: str
    adapter_type: str = "mcp"
    auth_token: Optional[str] = None


@router.get("/sources")
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    sources = db.query(McpSource).order_by(McpSource.created_at.desc()).all()
    return [
        {
            "id": s.id, "name": s.name, "url": s.url,
            "adapter_type": s.adapter_type, "is_active": s.is_active,
            "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
        }
        for s in sources
    ]


@router.post("/sources")
def create_source(
    req: McpSourceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = McpSource(
        name=req.name, url=req.url,
        adapter_type=req.adapter_type, auth_token=req.auth_token,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return {"id": source.id}


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()
    return {"ok": True}


@router.get("/search")
def search_market(
    source_id: int,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found or inactive")
    try:
        skills = list_remote_skills(source, q, page)
    except McpClientError as e:
        raise HTTPException(502, f"Remote source error: {e}")
    return skills


@router.get("/preview")
def preview_skill(
    source_id: int,
    upstream_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")
    try:
        skill_data = fetch_remote_skill(source, upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")
    return skill_data


class ImportRequest(BaseModel):
    source_id: int
    upstream_id: str


@router.post("/import")
def import_skill(
    req: ImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, req.source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")

    try:
        skill_data = fetch_remote_skill(source, req.upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")

    existing = (
        db.query(Skill)
        .filter(Skill.upstream_id == req.upstream_id, Skill.source_type.in_(["imported", "forked"]))
        .first()
    )
    if existing:
        raise HTTPException(409, f"Skill already imported (id={existing.id})")

    now = datetime.datetime.utcnow()
    skill = Skill(
        name=skill_data["name"],
        description=skill_data.get("description", ""),
        status=SkillStatus.DRAFT,
        source_type="imported",
        upstream_url=f"{source.url}/skills/{req.upstream_id}",
        upstream_id=req.upstream_id,
        upstream_version=skill_data.get("upstream_version", ""),
        upstream_content=skill_data.get("system_prompt", ""),
        upstream_synced_at=now,
        is_customized=False,
        created_by=user.id,
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=skill_data.get("system_prompt", ""),
        variables=[],
        created_by=user.id,
        change_note=f"从 {source.name} 导入 (upstream_id={req.upstream_id})",
    )
    db.add(version)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}
