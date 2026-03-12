"""Handoff 模板 & 缓存管理 API"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models.permission import (
    HandoffExecution,
    HandoffSchemaCache,
    HandoffTemplate,
    HandoffTemplateType,
)
from app.models.user import Role, User

router = APIRouter(prefix="/api/admin/handoff", tags=["handoff"])

_admin = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN))


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class HandoffTemplateCreate(BaseModel):
    name: str
    upstream_skill_id: Optional[int] = None
    downstream_skill_id: Optional[int] = None
    template_type: str = "standard"
    schema_fields: list = []
    excluded_fields: list = []


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _template(t: HandoffTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "upstream_skill_id": t.upstream_skill_id,
        "downstream_skill_id": t.downstream_skill_id,
        "template_type": t.template_type,
        "schema_fields": t.schema_fields or [],
        "excluded_fields": t.excluded_fields or [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _cache(c: HandoffSchemaCache) -> dict:
    return {
        "id": c.id,
        "cache_key": c.cache_key,
        "upstream_skill_id": c.upstream_skill_id,
        "downstream_skill_id": c.downstream_skill_id,
        "task_type_hash": c.task_type_hash,
        "hit_count": c.hit_count,
        "incomplete_count": c.incomplete_count,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "schema": c.schema_json,
    }


def _execution(e: HandoffExecution) -> dict:
    return {
        "id": e.id,
        "upstream_skill_id": e.upstream_skill_id,
        "downstream_skill_id": e.downstream_skill_id,
        "template_id": e.template_id,
        "cache_id": e.cache_id,
        "status": e.status,
        "error_msg": e.error_msg,
        "executed_by": e.executed_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ─── Template CRUD ────────────────────────────────────────────────────────────

@router.get("/templates")
def list_templates(
    upstream_skill_id: Optional[int] = None,
    downstream_skill_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(HandoffTemplate)
    if upstream_skill_id is not None:
        q = q.filter(HandoffTemplate.upstream_skill_id == upstream_skill_id)
    if downstream_skill_id is not None:
        q = q.filter(HandoffTemplate.downstream_skill_id == downstream_skill_id)
    return [_template(t) for t in q.all()]


@router.get("/templates/{template_id}")
def get_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    t = db.get(HandoffTemplate, template_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    return _template(t)


@router.post("/templates")
def create_template(
    req: HandoffTemplateCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    t = HandoffTemplate(**req.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return _template(t)


@router.put("/templates/{template_id}")
def update_template(
    template_id: int,
    req: HandoffTemplateCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    t = db.get(HandoffTemplate, template_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    for k, v in req.model_dump().items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return _template(t)


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    t = db.get(HandoffTemplate, template_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ─── Cache Management ─────────────────────────────────────────────────────────

@router.get("/caches")
def list_caches(
    upstream_skill_id: Optional[int] = None,
    downstream_skill_id: Optional[int] = None,
    expired: bool = False,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    import datetime
    q = db.query(HandoffSchemaCache)
    if upstream_skill_id is not None:
        q = q.filter(HandoffSchemaCache.upstream_skill_id == upstream_skill_id)
    if downstream_skill_id is not None:
        q = q.filter(HandoffSchemaCache.downstream_skill_id == downstream_skill_id)
    if not expired:
        q = q.filter(HandoffSchemaCache.expires_at > datetime.datetime.utcnow())
    return [_cache(c) for c in q.order_by(HandoffSchemaCache.hit_count.desc()).all()]


@router.post("/caches/{cache_id}/promote")
def promote_cache_to_template(
    cache_id: int,
    name: str = Query(..., description="新模板名称"),
    db: Session = Depends(get_db),
    user: User = _admin,
):
    """将高频缓存提升为静态模板"""
    c = db.get(HandoffSchemaCache, cache_id)
    if not c:
        raise HTTPException(404, "缓存不存在")

    schema = c.schema_json or {}
    schema_fields = schema.get("fields") or list(schema.keys())

    t = HandoffTemplate(
        name=name,
        upstream_skill_id=c.upstream_skill_id,
        downstream_skill_id=c.downstream_skill_id,
        template_type=HandoffTemplateType.STANDARD,
        schema_fields=schema_fields,
        excluded_fields=[],
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"ok": True, "template": _template(t)}


@router.delete("/caches/{cache_id}")
def delete_cache(
    cache_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    c = db.get(HandoffSchemaCache, cache_id)
    if not c:
        raise HTTPException(404, "缓存不存在")
    db.delete(c)
    db.commit()
    return {"ok": True}


# ─── Execution Records ────────────────────────────────────────────────────────

@router.get("/executions")
def list_executions(
    status: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    upstream_skill_id: Optional[int] = None,
    downstream_skill_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = _admin,
):
    import datetime
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    q = db.query(HandoffExecution).filter(HandoffExecution.created_at >= since)

    if status:
        q = q.filter(HandoffExecution.status == status)
    if upstream_skill_id is not None:
        q = q.filter(HandoffExecution.upstream_skill_id == upstream_skill_id)
    if downstream_skill_id is not None:
        q = q.filter(HandoffExecution.downstream_skill_id == downstream_skill_id)

    total = q.count()
    items = (
        q.order_by(HandoffExecution.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_execution(e) for e in items],
    }
