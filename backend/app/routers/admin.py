from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import require_role, get_current_user
from app.models.user import Department, Role, User
from app.models.skill import ModelConfig, ModelAssignment
from app.services.auth_service import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─── Model Config CRUD ───────────────────────────────────────────────────────

class ModelConfigCreate(BaseModel):
    name: str
    provider: str
    model_id: str
    api_base: str
    api_key_env: str = ""
    max_tokens: int = 4096
    temperature: str = "0.7"
    is_default: bool = False


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "employee"
    department_id: Optional[int] = None
    managed_department_id: Optional[int] = None
    position_id: Optional[int] = None
    is_active: bool = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    department_id: Optional[int] = None
    managed_department_id: Optional[int] = None
    position_id: Optional[int] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


@router.get("/models")
def list_models(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    return [
        {
            "id": m.id,
            "name": m.name,
            "provider": m.provider,
            "model_id": m.model_id,
            "api_base": m.api_base,
            "api_key_env": m.api_key_env,
            "max_tokens": m.max_tokens,
            "temperature": m.temperature,
            "is_default": m.is_default,
        }
        for m in db.query(ModelConfig).all()
    ]


@router.post("/models")
def create_model(
    req: ModelConfigCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    if req.is_default:
        db.query(ModelConfig).update({ModelConfig.is_default: False})
    mc = ModelConfig(**req.model_dump())
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return {"id": mc.id, "name": mc.name}


@router.put("/models/{model_id}")
def update_model(
    model_id: int,
    req: ModelConfigCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mc = db.get(ModelConfig, model_id)
    if not mc:
        raise HTTPException(404, "Model config not found")
    if req.is_default:
        db.query(ModelConfig).update({ModelConfig.is_default: False})
    for k, v in req.model_dump().items():
        setattr(mc, k, v)
    db.commit()
    db.refresh(mc)
    return {"id": mc.id, "name": mc.name}


# ─── Model Assignments (调用点 → 模型绑定) ──────────────────────────────────

@router.get("/model-assignments")
def list_model_assignments(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """返回全量调用点列表，合并 SLOT_REGISTRY 定义 + DB 中的实际绑定。"""
    from app.services.llm_gateway import SLOT_REGISTRY
    assignments = {a.slot_key: a for a in db.query(ModelAssignment).all()}
    result = []
    for key, slot in SLOT_REGISTRY.items():
        a = assignments.get(key)
        mc = a.model_config if a else None
        result.append({
            "slot_key": key,
            "name": slot["name"],
            "category": slot["category"],
            "desc": slot.get("desc", ""),
            "fallback": slot["fallback"],
            "model_config_id": a.model_config_id if a else None,
            "model_name": mc.name if mc else None,
        })
    return result


class ModelAssignmentUpdate(BaseModel):
    model_config_id: int


@router.put("/model-assignments/{slot_key:path}")
def set_model_assignment(
    slot_key: str,
    req: ModelAssignmentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """绑定调用点到指定模型。"""
    from app.services.llm_gateway import SLOT_REGISTRY
    if slot_key not in SLOT_REGISTRY:
        raise HTTPException(400, f"未知调用点: {slot_key}")
    mc = db.get(ModelConfig, req.model_config_id)
    if not mc:
        raise HTTPException(404, "模型配置不存在")
    a = db.query(ModelAssignment).filter_by(slot_key=slot_key).first()
    if a:
        a.model_config_id = req.model_config_id
    else:
        a = ModelAssignment(slot_key=slot_key, model_config_id=req.model_config_id)
        db.add(a)
    db.commit()
    return {"ok": True, "slot_key": slot_key, "model_config_id": req.model_config_id, "model_name": mc.name}


@router.delete("/model-assignments/{slot_key:path}")
def delete_model_assignment(
    slot_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """解绑调用点（恢复 fallback）。"""
    a = db.query(ModelAssignment).filter_by(slot_key=slot_key).first()
    if not a:
        raise HTTPException(404, "该调用点未绑定模型")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.delete("/models/{model_id}")
def delete_model(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mc = db.get(ModelConfig, model_id)
    if not mc:
        raise HTTPException(404, "Model config not found")
    db.delete(mc)
    db.commit()
    return {"ok": True}


# ─── Department listing (for user management UI) ─────────────────────────────

@router.get("/departments")
def list_departments(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return [
        {
            "id": d.id,
            "name": d.name,
            "parent_id": d.parent_id,
            "category": d.category,
            "business_unit": d.business_unit,
        }
        for d in db.query(Department).all()
    ]


# ─── User CRUD ────────────────────────────────────────────────────────────────

def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role.value if hasattr(u.role, "value") else u.role,
        "department_id": u.department_id,
        "department_name": u.department.name if u.department else None,
        "managed_department_id": u.managed_department_id,
        "managed_department_name": u.managed_department.name if u.managed_department else None,
        "position_id": u.position_id,
        "position_name": u.position.name if u.position else None,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/users")
def list_users(
    department_id: Optional[int] = None,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    q = db.query(User)
    if department_id is not None:
        q = q.filter(User.department_id == department_id)
    elif current.role == Role.DEPT_ADMIN:
        # 部门管理员看管辖部门及所有子部门
        managed_ids = current.get_managed_department_ids(db)
        if managed_ids:
            q = q.filter(User.department_id.in_(managed_ids))
        else:
            q = q.filter(User.department_id == current.department_id)
    if role:
        q = q.filter(User.role == role)
    if is_active is not None:
        q = q.filter(User.is_active == is_active)
    return [_user_dict(u) for u in q.order_by(User.id).all()]


@router.post("/users")
def create_user(
    req: UserCreate,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    existing = db.query(User).filter(User.username == req.username).first()
    if existing:
        raise HTTPException(400, f"用户名 {req.username!r} 已存在")
    # dept_admin 只能在管辖范围内创建员工
    if current.role == Role.DEPT_ADMIN:
        if req.role not in ("employee",):
            raise HTTPException(403, "部门管理员只能创建普通员工")
        managed_ids = current.get_managed_department_ids(db)
        if req.department_id and managed_ids and req.department_id not in managed_ids:
            raise HTTPException(403, "只能在管辖部门内创建用户")
        elif req.department_id and not managed_ids and req.department_id != current.department_id:
            raise HTTPException(403, "只能在本部门内创建用户")
    u = User(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
        role=req.role,
        department_id=req.department_id,
        position_id=req.position_id,
        is_active=req.is_active,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


@router.put("/users/{uid}")
def update_user(
    uid: int,
    req: UserUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    # dept_admin 只能编辑管辖范围内的员工
    if current.role == Role.DEPT_ADMIN:
        managed_ids = current.get_managed_department_ids(db)
        if managed_ids:
            if u.department_id not in managed_ids:
                raise HTTPException(403, "无权编辑管辖范围外的用户")
        elif u.department_id != current.department_id:
            raise HTTPException(403, "无权编辑其他部门用户")
        if req.role and req.role != "employee":
            raise HTTPException(403, "部门管理员无法修改角色")
    data = req.model_dump(exclude_none=True)
    if "password" in data:
        u.password_hash = hash_password(data.pop("password"))
    for k, v in data.items():
        setattr(u, k, v)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


@router.get("/users/suggested")
def suggested_users(
    q: Optional[str] = None,
    exclude: Optional[str] = None,  # 逗号分隔的 user_id 列表
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """返回推荐同事列表（合作次数 > 二级部门 > 一级部门 > BU），最多6个。支持 q 搜索。"""
    from app.models.project import ProjectMember

    exclude_ids: set[int] = set()
    if exclude:
        for x in exclude.split(","):
            try:
                exclude_ids.add(int(x.strip()))
            except ValueError:
                pass

    # ── 搜索模式：q 参数时直接按名字/用户名模糊搜索 ──────────────────────────
    if q and q.strip():
        keyword = q.strip().lstrip("@")
        users = (
            db.query(User)
            .filter(
                User.is_active == True,
                User.id.notin_(exclude_ids),
                (User.display_name.ilike(f"%{keyword}%") | User.username.ilike(f"%{keyword}%")),
            )
            .limit(10)
            .all()
        )
        return [_suggested_user_dict(u, "search") for u in users]

    # ── 推荐模式：优先合作过的同事 ──────────────────────────────────────────
    # 1. 找当前用户参与过的所有项目
    my_project_ids = [
        row.project_id
        for row in db.query(ProjectMember.project_id)
        .filter(ProjectMember.user_id == current.id)
        .all()
    ]

    # 2. 统计同一项目中其他成员出现次数（合作次数）
    collab_counts: dict[int, int] = {}
    if my_project_ids:
        rows = (
            db.query(ProjectMember.user_id)
            .filter(
                ProjectMember.project_id.in_(my_project_ids),
                ProjectMember.user_id.notin_(exclude_ids),
            )
            .all()
        )
        for (uid,) in rows:
            collab_counts[uid] = collab_counts.get(uid, 0) + 1

    # 3. 获取这些合作用户（按合作次数降序，最多6个）
    collab_users: list[tuple[User, str]] = []
    if collab_counts:
        top_ids = sorted(collab_counts, key=lambda x: -collab_counts[x])[:6]
        for uid in top_ids:
            u = db.get(User, uid)
            if u and u.is_active:
                collab_users.append((u, f"合作 {collab_counts[uid]} 次"))

    if len(collab_users) >= 6:
        return [_suggested_user_dict(u, hint) for u, hint in collab_users[:6]]

    # 4. 不足6个，按部门层级补充
    already_ids = exclude_ids | {u.id for u, _ in collab_users}

    def fill_from_dept(dept_ids: set[int], hint: str) -> list[tuple[User, str]]:
        if not dept_ids:
            return []
        return [
            (u, hint)
            for u in db.query(User)
            .filter(
                User.department_id.in_(dept_ids),
                User.is_active == True,
                User.id.notin_(already_ids),
            )
            .limit(6)
            .all()
        ]

    # 获取当前用户部门的层级信息
    my_dept = db.get(Department, current.department_id) if current.department_id else None

    # 二级部门（当前用户所在的部门本身）
    if my_dept and len(collab_users) < 6:
        same_dept = fill_from_dept({my_dept.id}, "同部门")
        for item in same_dept:
            if len(collab_users) >= 6:
                break
            collab_users.append(item)
            already_ids.add(item[0].id)

    # 一级部门（parent）
    if my_dept and my_dept.parent_id and len(collab_users) < 6:
        parent_dept = db.get(Department, my_dept.parent_id)
        if parent_dept:
            # 一级部门下的所有二级部门
            sibling_dept_ids = {
                d.id for d in db.query(Department).filter(Department.parent_id == parent_dept.id).all()
            }
            sibling_dept_ids.add(parent_dept.id)
            siblings = fill_from_dept(sibling_dept_ids, "同一级部门")
            for item in siblings:
                if len(collab_users) >= 6:
                    break
                collab_users.append(item)
                already_ids.add(item[0].id)

    # BU（business_unit）
    if my_dept and my_dept.business_unit and len(collab_users) < 6:
        bu_dept_ids = {
            d.id for d in db.query(Department).filter(Department.business_unit == my_dept.business_unit).all()
        }
        bu_users = fill_from_dept(bu_dept_ids, "同 BU")
        for item in bu_users:
            if len(collab_users) >= 6:
                break
            collab_users.append(item)
            already_ids.add(item[0].id)

    return [_suggested_user_dict(u, hint) for u, hint in collab_users[:6]]


def _suggested_user_dict(u: User, hint: str) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "department_name": u.department.name if u.department else None,
        "hint": hint,
    }


@router.delete("/users/{uid}")
def deactivate_user(
    uid: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管专属：停用用户（软删除）"""
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.id == current.id:
        raise HTTPException(400, "不能停用自己")
    u.is_active = False
    db.commit()
    return {"ok": True}


# ─── Model Grants（受限模型授权）─────────────────────────────────────────────

@router.get("/model-grants")
def list_model_grants(
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """列出所有受限模型授权记录。"""
    from app.models.opencode import UserModelGrant
    grants = db.query(UserModelGrant).all()
    return [
        {
            "id": g.id,
            "user_id": g.user_id,
            "display_name": g.user.display_name if g.user else None,
            "model_key": g.model_key,
            "granted_by": g.granted_by,
            "granted_at": g.granted_at.isoformat() if g.granted_at else None,
        }
        for g in grants
    ]


@router.post("/model-grants/{uid}")
def grant_model(
    uid: int,
    model_key: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """为指定用户授权使用受限模型。"""
    from app.models.opencode import UserModelGrant
    from sqlalchemy.exc import IntegrityError
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    grant = UserModelGrant(user_id=uid, model_key=model_key, granted_by=current.id)
    db.add(grant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "已授权")
    return {"ok": True, "user_id": uid, "model_key": model_key}


@router.delete("/model-grants/{uid}")
def revoke_model(
    uid: int,
    model_key: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """撤销指定用户的受限模型授权。"""
    from app.models.opencode import UserModelGrant
    grant = (
        db.query(UserModelGrant)
        .filter(UserModelGrant.user_id == uid, UserModelGrant.model_key == model_key)
        .first()
    )
    if not grant:
        raise HTTPException(404, "授权记录不存在")
    db.delete(grant)
    db.commit()
    return {"ok": True}


_DEFAULT_FEATURE_FLAGS = {
    "dev_studio": True,
    "asr": True,
    "webapp_publish": True,
    "batch_upload_skill": False,
    "feishu_sync": False,
}


class FeatureFlagsUpdate(BaseModel):
    feature_flags: dict


@router.get("/users/{uid}/features")
def get_user_features(
    uid: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """获取指定用户的功能开关。"""
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    flags = {**_DEFAULT_FEATURE_FLAGS, **(user.feature_flags or {})}
    return {"feature_flags": flags}


@router.put("/users/{uid}/features")
def update_user_features(
    uid: int,
    body: FeatureFlagsUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """更新指定用户的功能开关（仅超管）。"""
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    # 只允许已知 key
    allowed_keys = set(_DEFAULT_FEATURE_FLAGS.keys())
    filtered = {k: bool(v) for k, v in body.feature_flags.items() if k in allowed_keys}
    user.feature_flags = {**(user.feature_flags or {}), **filtered}
    db.commit()
    db.refresh(user)
    flags = {**_DEFAULT_FEATURE_FLAGS, **(user.feature_flags or {})}
    return {"ok": True, "feature_flags": flags}
