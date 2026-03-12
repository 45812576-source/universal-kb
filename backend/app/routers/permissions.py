"""数据权限配置 API — positions / data-domains / policies / users / 脱敏规则"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.business import BusinessTable
from app.models.permission import (
    DataDomain,
    DataScopePolicy,
    GlobalDataMask,
    MaskAction,
    Position,
    RoleMaskOverride,
    RoleOutputMask,
)
from app.models.user import Department, Role, User

router = APIRouter(prefix="/api/admin/permissions", tags=["permissions"])

_admin = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN))


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class PositionCreate(BaseModel):
    name: str
    department_id: Optional[int] = None
    description: Optional[str] = None


class DataDomainCreate(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = None
    fields: List[dict] = []


class PolicyCreate(BaseModel):
    target_type: str  # "position" | "role"
    target_position_id: Optional[int] = None
    target_role: Optional[str] = None
    resource_type: str  # "business_table" | "data_domain"
    business_table_id: Optional[int] = None
    data_domain_id: Optional[int] = None
    visibility_level: str = "own"  # "own" | "dept" | "all"
    output_mask: List[str] = []


class UserPositionUpdate(BaseModel):
    position_id: Optional[int] = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _pos(p: Position) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "department_id": p.department_id,
        "department_name": p.department.name if p.department else None,
        "description": p.description,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _domain(d: DataDomain) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "display_name": d.display_name,
        "description": d.description,
        "fields": d.fields or [],
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _policy(p: DataScopePolicy, db: Session) -> dict:
    bt_name = None
    if p.business_table_id:
        bt = db.get(BusinessTable, p.business_table_id)
        bt_name = bt.display_name if bt else None
    dd_name = None
    if p.data_domain_id:
        dd = db.get(DataDomain, p.data_domain_id)
        dd_name = dd.display_name if dd else None
    pos_name = None
    if p.target_position_id:
        pos = db.get(Position, p.target_position_id)
        pos_name = pos.name if pos else None
    return {
        "id": p.id,
        "target_type": p.target_type,
        "target_position_id": p.target_position_id,
        "target_position_name": pos_name,
        "target_role": p.target_role,
        "resource_type": p.resource_type,
        "business_table_id": p.business_table_id,
        "business_table_name": bt_name,
        "data_domain_id": p.data_domain_id,
        "data_domain_name": dd_name,
        "visibility_level": p.visibility_level,
        "output_mask": p.output_mask or [],
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ─── Positions ───────────────────────────────────────────────────────────────

@router.get("/positions")
def list_positions(
    department_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(Position)
    if department_id is not None:
        q = q.filter(Position.department_id == department_id)
    return [_pos(p) for p in q.all()]


@router.post("/positions")
def create_position(
    req: PositionCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = Position(**req.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _pos(p)


@router.put("/positions/{pid}")
def update_position(
    pid: int,
    req: PositionCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(Position, pid)
    if not p:
        raise HTTPException(404, "岗位不存在")
    for k, v in req.model_dump().items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _pos(p)


@router.delete("/positions/{pid}")
def delete_position(
    pid: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(Position, pid)
    if not p:
        raise HTTPException(404, "岗位不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ─── Data Domains ────────────────────────────────────────────────────────────

@router.get("/data-domains")
def list_data_domains(
    db: Session = Depends(get_db),
    user: User = _admin,
):
    return [_domain(d) for d in db.query(DataDomain).all()]


@router.post("/data-domains")
def create_data_domain(
    req: DataDomainCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    d = DataDomain(**req.model_dump())
    db.add(d)
    db.commit()
    db.refresh(d)
    return _domain(d)


@router.put("/data-domains/{did}")
def update_data_domain(
    did: int,
    req: DataDomainCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    d = db.get(DataDomain, did)
    if not d:
        raise HTTPException(404, "数据域不存在")
    for k, v in req.model_dump().items():
        setattr(d, k, v)
    db.commit()
    db.refresh(d)
    return _domain(d)


@router.delete("/data-domains/{did}")
def delete_data_domain(
    did: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    d = db.get(DataDomain, did)
    if not d:
        raise HTTPException(404, "数据域不存在")
    db.delete(d)
    db.commit()
    return {"ok": True}


# ─── Policies ────────────────────────────────────────────────────────────────

@router.get("/policies")
def list_policies(
    target_type: Optional[str] = None,
    target_position_id: Optional[int] = None,
    target_role: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(DataScopePolicy)
    if target_type:
        q = q.filter(DataScopePolicy.target_type == target_type)
    if target_position_id is not None:
        q = q.filter(DataScopePolicy.target_position_id == target_position_id)
    if target_role:
        q = q.filter(DataScopePolicy.target_role == target_role)
    return [_policy(p, db) for p in q.all()]


@router.post("/policies")
def create_policy(
    req: PolicyCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = DataScopePolicy(**req.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _policy(p, db)


@router.put("/policies/{pid}")
def update_policy(
    pid: int,
    req: PolicyCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(DataScopePolicy, pid)
    if not p:
        raise HTTPException(404, "策略不存在")
    for k, v in req.model_dump().items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _policy(p, db)


@router.delete("/policies/{pid}")
def delete_policy(
    pid: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    p = db.get(DataScopePolicy, pid)
    if not p:
        raise HTTPException(404, "策略不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ─── Users (含 position_id) ──────────────────────────────────────────────────

@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    user: User = _admin,
):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role,
            "department_id": u.department_id,
            "department_name": u.department.name if u.department else None,
            "position_id": u.position_id,
            "position_name": u.position.name if u.position else None,
            "is_active": u.is_active,
        }
        for u in users
    ]


@router.put("/users/{uid}")
def update_user_position(
    uid: int,
    req: UserPositionUpdate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    u.position_id = req.position_id
    db.commit()
    return {"ok": True}


# ─── Business table columns (for output_mask selector) ───────────────────────

# ─── Global Data Masks ───────────────────────────────────────────────────────

class GlobalMaskCreate(BaseModel):
    field_name: str
    data_domain_id: Optional[int] = None
    mask_action: str = "hide"
    mask_params: dict = {}
    severity: int = 1


def _global_mask(m: GlobalDataMask) -> dict:
    return {
        "id": m.id,
        "field_name": m.field_name,
        "data_domain_id": m.data_domain_id,
        "mask_action": m.mask_action,
        "mask_params": m.mask_params or {},
        "severity": m.severity,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/global-masks")
def list_global_masks(
    data_domain_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(GlobalDataMask)
    if data_domain_id is not None:
        q = q.filter(GlobalDataMask.data_domain_id == data_domain_id)
    return [_global_mask(m) for m in q.all()]


@router.post("/global-masks")
def create_global_mask(
    req: GlobalMaskCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = GlobalDataMask(**req.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _global_mask(m)


@router.put("/global-masks/{mid}")
def update_global_mask(
    mid: int,
    req: GlobalMaskCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = db.get(GlobalDataMask, mid)
    if not m:
        raise HTTPException(404, "规则不存在")
    for k, v in req.model_dump().items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return _global_mask(m)


@router.delete("/global-masks/{mid}")
def delete_global_mask(
    mid: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = db.get(GlobalDataMask, mid)
    if not m:
        raise HTTPException(404, "规则不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ─── Role Mask Overrides ──────────────────────────────────────────────────────

class RoleMaskCreate(BaseModel):
    position_id: int
    field_name: str
    data_domain_id: Optional[int] = None
    mask_action: str
    mask_params: dict = {}


def _role_mask(m: RoleMaskOverride) -> dict:
    return {
        "id": m.id,
        "position_id": m.position_id,
        "field_name": m.field_name,
        "data_domain_id": m.data_domain_id,
        "mask_action": m.mask_action,
        "mask_params": m.mask_params or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/role-masks")
def list_role_masks(
    position_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(RoleMaskOverride)
    if position_id is not None:
        q = q.filter(RoleMaskOverride.position_id == position_id)
    return [_role_mask(m) for m in q.all()]


@router.post("/role-masks")
def create_role_mask(
    req: RoleMaskCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = RoleMaskOverride(**req.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _role_mask(m)


@router.delete("/role-masks/{mid}")
def delete_role_mask(
    mid: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = db.get(RoleMaskOverride, mid)
    if not m:
        raise HTTPException(404, "覆盖规则不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ─── Role Output Masks ────────────────────────────────────────────────────────

class OutputMaskCreate(BaseModel):
    position_id: int
    data_domain_id: int
    field_name: str
    mask_action: str = "show"


def _output_mask(m: RoleOutputMask) -> dict:
    return {
        "id": m.id,
        "position_id": m.position_id,
        "data_domain_id": m.data_domain_id,
        "field_name": m.field_name,
        "mask_action": m.mask_action,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/output-masks")
def list_output_masks(
    position_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(RoleOutputMask)
    if position_id is not None:
        q = q.filter(RoleOutputMask.position_id == position_id)
    return [_output_mask(m) for m in q.all()]


@router.post("/output-masks")
def create_output_mask(
    req: OutputMaskCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = RoleOutputMask(**req.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _output_mask(m)


@router.put("/output-masks/{mid}")
def update_output_mask(
    mid: int,
    req: OutputMaskCreate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    m = db.get(RoleOutputMask, mid)
    if not m:
        raise HTTPException(404, "遮罩规则不存在")
    for k, v in req.model_dump().items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return _output_mask(m)


# ─── Mask Preview ─────────────────────────────────────────────────────────────

class MaskPreviewRequest(BaseModel):
    position_id: int
    data_domain_id: Optional[int] = None
    skill_id: Optional[int] = None
    sample_data: List[dict]


@router.post("/mask-preview")
def mask_preview(
    req: MaskPreviewRequest,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    """三层合并脱敏预览：传入岗位/数据域/示例数据，返回脱敏效果"""
    from app.services.permission_engine import permission_engine

    # 构造一个临时 user-like 对象
    class _FakeUser:
        role = Role.EMPLOYEE
        position_id = req.position_id
        department_id = None
        id = 0

    fake_user = _FakeUser()
    skill_id = req.skill_id or 0

    masked = permission_engine.apply_data_masks(
        user=fake_user,
        skill_id=skill_id,
        data=req.sample_data,
        data_domain_id=req.data_domain_id,
        db=db,
    )
    return {"masked": masked, "original_count": len(req.sample_data)}


# ─── Business table columns (for output_mask selector) ───────────────────────

@router.get("/business-table-columns/{table_id}")
def get_table_columns(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    from sqlalchemy import text
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "业务表不存在")
    try:
        sql = text("""
            SELECT COLUMN_NAME, DATA_TYPE, COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
        """)
        rows = db.execute(sql, {"table_name": bt.table_name}).fetchall()
        columns = [{"name": r[0], "type": r[1], "comment": r[2] or ""} for r in rows]
    except Exception:
        columns = []
    return {"table_id": table_id, "table_name": bt.table_name, "columns": columns}
