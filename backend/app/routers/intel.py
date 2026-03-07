"""Intel (intelligence) collection API."""
import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.intel import IntelEntry, IntelEntryStatus, IntelSource, IntelSourceType
from app.models.user import Role, User

router = APIRouter(prefix="/api/intel", tags=["intel"])


# --- Schemas ---

class SourceCreate(BaseModel):
    name: str
    source_type: IntelSourceType
    config: Optional[dict] = None
    schedule: Optional[str] = None
    is_active: bool = True


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict] = None
    schedule: Optional[str] = None
    is_active: Optional[bool] = None


def _source_dict(s: IntelSource) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "source_type": s.source_type.value,
        "config": s.config,
        "schedule": s.schedule,
        "is_active": s.is_active,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _entry_dict(e: IntelEntry) -> dict:
    return {
        "id": e.id,
        "source_id": e.source_id,
        "title": e.title,
        "content": (e.content or "")[:500] + ("..." if e.content and len(e.content) > 500 else ""),
        "url": e.url,
        "tags": e.tags or [],
        "industry": e.industry,
        "platform": e.platform,
        "status": e.status.value,
        "auto_collected": e.auto_collected,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "approved_at": e.approved_at.isoformat() if e.approved_at else None,
    }


# --- Source Management (Admin) ---

@router.get("/sources")
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    sources = db.query(IntelSource).order_by(IntelSource.created_at.desc()).all()
    return [_source_dict(s) for s in sources]


@router.post("/sources")
def create_source(
    body: SourceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = IntelSource(
        name=body.name,
        source_type=body.source_type,
        config=body.config or {},
        schedule=body.schedule,
        is_active=body.is_active,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return _source_dict(source)


@router.put("/sources/{source_id}")
def update_source(
    source_id: int,
    body: SourceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(IntelSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(source, field, value)
    db.commit()
    db.refresh(source)
    return _source_dict(source)


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = db.get(IntelSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    db.delete(source)
    db.commit()
    return {"ok": True}


@router.post("/sources/{source_id}/run")
async def trigger_source(
    source_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Manually trigger collection for a source."""
    source = db.get(IntelSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from app.services.intel_collector import intel_collector
    from app.database import SessionLocal

    async def _run():
        dbs = SessionLocal()
        try:
            count = await intel_collector.run_source(dbs, source)
            return count
        finally:
            dbs.close()

    background_tasks.add_task(_run)
    return {"ok": True, "message": "采集任务已在后台启动"}


# --- Entry Management ---

@router.get("/entries")
def list_entries(
    status: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(IntelEntry)

    # Non-admins only see approved entries
    from app.models.user import Role as UserRole
    is_admin = user.role in (UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN)
    if not is_admin:
        query = query.filter(IntelEntry.status == IntelEntryStatus.APPROVED)
    elif status:
        try:
            query = query.filter(IntelEntry.status == IntelEntryStatus(status))
        except ValueError:
            pass

    if industry:
        query = query.filter(IntelEntry.industry == industry)
    if platform:
        query = query.filter(IntelEntry.platform == platform)
    if q:
        query = query.filter(
            IntelEntry.title.contains(q) | IntelEntry.content.contains(q)
        )

    total = query.count()
    entries = (
        query.order_by(IntelEntry.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "page": page, "page_size": page_size, "items": [_entry_dict(e) for e in entries]}


@router.get("/entries/{entry_id}")
def get_entry(entry_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    entry = db.get(IntelEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # Non-admins can only see approved
    from app.models.user import Role as UserRole
    is_admin = user.role in (UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN)
    if not is_admin and entry.status != IntelEntryStatus.APPROVED:
        raise HTTPException(status_code=403, detail="Not authorized")
    d = _entry_dict(entry)
    d["content"] = entry.content  # Full content for detail view
    return d


@router.patch("/entries/{entry_id}/approve")
def approve_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    entry = db.get(IntelEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.status = IntelEntryStatus.APPROVED
    entry.approved_at = datetime.datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.patch("/entries/{entry_id}/reject")
def reject_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    entry = db.get(IntelEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.status = IntelEntryStatus.REJECTED
    db.commit()
    return {"ok": True}


@router.post("/entries")
def create_entry(
    title: str,
    content: Optional[str] = None,
    url: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Manually create an intel entry."""
    entry = IntelEntry(
        title=title,
        content=content,
        url=url,
        status=IntelEntryStatus.PENDING,
        auto_collected=False,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _entry_dict(entry)
