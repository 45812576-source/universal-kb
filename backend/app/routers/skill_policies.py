"""Skill 权限策略管理 API"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models.permission import (
    ConnectionDirection,
    RolePolicyOverride,
    SkillAgentConnection,
    SkillMaskOverride,
    SkillPolicy,
)
from app.models.skill import Skill
from app.models.user import Role, User

router = APIRouter(prefix="/api/admin/skill-policies", tags=["skill-policies"])

_admin = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN))


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class SkillPolicyCreate(BaseModel):
    skill_id: int
    publish_scope: str = "same_role"
    default_data_scope: dict = {}


class SkillPolicyUpdate(BaseModel):
    publish_scope: Optional[str] = None
    default_data_scope: Optional[dict] = None


class RolePolicyOverrideCreate(BaseModel):
    position_id: int
    callable: bool = True
    data_scope: dict = {}
    output_mask: list = []


class SkillMaskOverrideCreate(BaseModel):
    position_id: Optional[int] = None
    field_name: str
    mask_action: str
    mask_params: dict = {}


class AgentConnectionCreate(BaseModel):
    direction: str  # "upstream" | "downstream"
    connected_skill_id: int


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _policy(p: SkillPolicy) -> dict:
    return {
        "id": p.id,
        "skill_id": p.skill_id,
        "publish_scope": p.publish_scope,
        "default_data_scope": p.default_data_scope or {},
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _override(o: RolePolicyOverride) -> dict:
    return {
        "id": o.id,
        "skill_policy_id": o.skill_policy_id,
        "position_id": o.position_id,
        "callable": o.callable,
        "data_scope": o.data_scope or {},
        "output_mask": o.output_mask or [],
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def _skill_mask(m: SkillMaskOverride) -> dict:
    return {
        "id": m.id,
        "skill_id": m.skill_id,
        "position_id": m.position_id,
        "field_name": m.field_name,
        "mask_action": m.mask_action,
        "mask_params": m.mask_params or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _connection(c: SkillAgentConnection) -> dict:
    return {
        "id": c.id,
        "skill_policy_id": c.skill_policy_id,
        "direction": c.direction,
        "connected_skill_id": c.connected_skill_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ─── Skill Policy CRUD ────────────────────────────────────────────────────────

@router.get("")
def list_skill_policies(
    skill_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(SkillPolicy)
    if skill_id is not None:
        q = q.filter(SkillPolicy.skill_id == skill_id)
    return [_policy(p) for p in q.all()]


@router.post("")
def create_skill_policy(
    req: SkillPolicyCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    skill = db.get(Skill, req.skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    existing = db.query(SkillPolicy).filter(SkillPolicy.skill_id == req.skill_id).first()
    if existing:
        raise HTTPException(400, f"Skill {req.skill_id} 已有 Policy，请使用 PUT 更新")
    p = SkillPolicy(**req.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _policy(p)


@router.put("/{policy_id}")
def update_skill_policy(
    policy_id: int,
    req: SkillPolicyUpdate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    data = req.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _policy(p)


# ─── Role Policy Overrides ────────────────────────────────────────────────────

@router.get("/{policy_id}/overrides")
def list_overrides(
    policy_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    return [_override(o) for o in p.overrides]


@router.post("/{policy_id}/overrides")
def create_override(
    policy_id: int,
    req: RolePolicyOverrideCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    existing = (
        db.query(RolePolicyOverride)
        .filter(
            RolePolicyOverride.skill_policy_id == policy_id,
            RolePolicyOverride.position_id == req.position_id,
        )
        .first()
    )
    if existing:
        for k, v in req.model_dump().items():
            setattr(existing, k, v)
        db.commit()
        db.refresh(existing)
        return _override(existing)
    o = RolePolicyOverride(skill_policy_id=policy_id, **req.model_dump())
    db.add(o)
    db.commit()
    db.refresh(o)
    return _override(o)


@router.put("/{policy_id}/overrides/{override_id}")
def update_override(
    policy_id: int,
    override_id: int,
    req: RolePolicyOverrideCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    o = db.get(RolePolicyOverride, override_id)
    if not o or o.skill_policy_id != policy_id:
        raise HTTPException(404, "Override 不存在")
    for k, v in req.model_dump().items():
        setattr(o, k, v)
    db.commit()
    db.refresh(o)
    return _override(o)


@router.delete("/{policy_id}/overrides/{override_id}")
def delete_override(
    policy_id: int,
    override_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    o = db.get(RolePolicyOverride, override_id)
    if not o or o.skill_policy_id != policy_id:
        raise HTTPException(404, "Override 不存在")
    db.delete(o)
    db.commit()
    return {"ok": True}


# ─── Skill Mask Overrides ─────────────────────────────────────────────────────

@router.get("/{policy_id}/masks")
def list_skill_masks(
    policy_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    masks = (
        db.query(SkillMaskOverride)
        .filter(SkillMaskOverride.skill_id == p.skill_id)
        .all()
    )
    return [_skill_mask(m) for m in masks]


@router.post("/{policy_id}/masks")
def set_skill_masks(
    policy_id: int,
    req: list[SkillMaskOverrideCreate],
    db: Session = Depends(get_db),
    user: User = _admin,
):
    """批量设置 Skill 级脱敏覆盖（upsert）"""
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")

    result = []
    for item in req:
        existing = (
            db.query(SkillMaskOverride)
            .filter(
                SkillMaskOverride.skill_id == p.skill_id,
                SkillMaskOverride.field_name == item.field_name,
                SkillMaskOverride.position_id == item.position_id,
            )
            .first()
        )
        if existing:
            existing.mask_action = item.mask_action
            existing.mask_params = item.mask_params
            db.flush()
            result.append(_skill_mask(existing))
        else:
            m = SkillMaskOverride(skill_id=p.skill_id, **item.model_dump())
            db.add(m)
            db.flush()
            result.append(_skill_mask(m))
    db.commit()
    return result


@router.delete("/{policy_id}/masks/{mask_id}")
def delete_skill_mask(
    policy_id: int,
    mask_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    m = db.get(SkillMaskOverride, mask_id)
    if not m or m.skill_id != p.skill_id:
        raise HTTPException(404, "遮罩规则不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ─── Agent Connections ────────────────────────────────────────────────────────

@router.get("/{policy_id}/connections")
def list_connections(
    policy_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    return [_connection(c) for c in p.agent_connections]


@router.post("/{policy_id}/connections")
def add_connection(
    policy_id: int,
    req: AgentConnectionCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(SkillPolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy 不存在")
    connected = db.get(Skill, req.connected_skill_id)
    if not connected:
        raise HTTPException(404, "目标 Skill 不存在")
    existing = (
        db.query(SkillAgentConnection)
        .filter(
            SkillAgentConnection.skill_policy_id == policy_id,
            SkillAgentConnection.direction == req.direction,
            SkillAgentConnection.connected_skill_id == req.connected_skill_id,
        )
        .first()
    )
    if existing:
        return _connection(existing)
    c = SkillAgentConnection(skill_policy_id=policy_id, **req.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return _connection(c)


@router.delete("/{policy_id}/connections/{connection_id}")
def delete_connection(
    policy_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    c = db.get(SkillAgentConnection, connection_id)
    if not c or c.skill_policy_id != policy_id:
        raise HTTPException(404, "连接不存在")
    db.delete(c)
    db.commit()
    return {"ok": True}
