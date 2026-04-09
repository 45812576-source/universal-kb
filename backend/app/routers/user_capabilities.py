"""用户资格能力 CRUD

GET    /admin/users/{uid}/capabilities       — 列出用户全部资格
POST   /admin/users/{uid}/capabilities       — 授予资格
DELETE /admin/users/{uid}/capabilities/{gid}  — 回收资格
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models.user import User, Role
from app.models.user_capability import UserCapabilityGrant
from app.routers.admin import require_role

router = APIRouter(prefix="/api/admin/users", tags=["user-capabilities"])


class CapabilityGrantCreate(BaseModel):
    capability_key: str
    source: str = "direct"
    scope_json: Optional[dict] = None


def _serialize(g: UserCapabilityGrant) -> dict:
    return {
        "id": g.id,
        "user_id": g.user_id,
        "capability_key": g.capability_key,
        "granted_by": g.granted_by,
        "granted_at": g.granted_at.isoformat() if g.granted_at else None,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "source": g.source,
        "scope_json": g.scope_json,
    }


@router.get("/{uid}/capabilities")
def list_capabilities(
    uid: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    grants = (
        db.query(UserCapabilityGrant)
        .filter(UserCapabilityGrant.user_id == uid)
        .order_by(UserCapabilityGrant.granted_at.desc())
        .all()
    )
    return [_serialize(g) for g in grants]


@router.post("/{uid}/capabilities")
def grant_capability(
    uid: int,
    req: CapabilityGrantCreate,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    # Check for duplicate
    existing = (
        db.query(UserCapabilityGrant)
        .filter(
            UserCapabilityGrant.user_id == uid,
            UserCapabilityGrant.capability_key == req.capability_key,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, f"用户已拥有资格 {req.capability_key}")

    grant = UserCapabilityGrant(
        user_id=uid,
        capability_key=req.capability_key,
        granted_by=current.id,
        source=req.source,
        scope_json=req.scope_json,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return _serialize(grant)


@router.delete("/{uid}/capabilities/{gid}")
def revoke_capability(
    uid: int,
    gid: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    grant = db.get(UserCapabilityGrant, gid)
    if not grant or grant.user_id != uid:
        raise HTTPException(404, "资格授权不存在")
    db.delete(grant)
    db.commit()
    return {"ok": True}
