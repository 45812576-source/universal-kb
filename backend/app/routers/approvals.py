"""审批流 API"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.permission import (
    ApprovalAction,
    ApprovalActionType,
    ApprovalRequest,
    ApprovalRequestType,
    ApprovalStatus,
)
from app.models.user import Role, User

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class ApprovalRequestCreate(BaseModel):
    request_type: str
    target_id: Optional[int] = None
    target_type: Optional[str] = None


class ApprovalActionCreate(BaseModel):
    action: str  # "approve" | "reject" | "add_conditions"
    comment: Optional[str] = None
    conditions: Optional[list] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _req(r: ApprovalRequest, db: Session) -> dict:
    return {
        "id": r.id,
        "request_type": r.request_type,
        "target_id": r.target_id,
        "target_type": r.target_type,
        "requester_id": r.requester_id,
        "requester_name": r.requester.display_name if r.requester else None,
        "status": r.status,
        "conditions": r.conditions or [],
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "actions": [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "actor_name": a.actor.display_name if a.actor else None,
                "action": a.action,
                "comment": a.comment,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in (r.actions or [])
        ],
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_approvals(
    status: Optional[str] = Query(None),
    request_type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待审批列表。管理员看全部，部门负责人可看本部门请求，员工看自己的。"""
    q = db.query(ApprovalRequest)

    # 权限过滤
    if user.role == Role.EMPLOYEE:
        q = q.filter(ApprovalRequest.requester_id == user.id)
    elif user.role == Role.DEPT_ADMIN:
        # 部门负责人看：自己发起的 + 本部门用户发起的
        from app.models.user import User as UserModel
        dept_user_ids = [
            u.id for u in db.query(UserModel).filter(UserModel.department_id == user.department_id).all()
        ]
        from sqlalchemy import or_
        q = q.filter(
            or_(
                ApprovalRequest.requester_id == user.id,
                ApprovalRequest.requester_id.in_(dept_user_ids),
            )
        )
    # super_admin 看全部，不加过滤

    if status:
        q = q.filter(ApprovalRequest.status == status)
    if request_type:
        q = q.filter(ApprovalRequest.request_type == request_type)

    total = q.count()
    items = (
        q.order_by(ApprovalRequest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_req(r, db) for r in items],
    }


@router.get("/my")
def my_approvals(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """我发起的审批"""
    items = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.requester_id == user.id)
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    return [_req(r, db) for r in items]


@router.get("/{request_id}")
def get_approval(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    r = db.get(ApprovalRequest, request_id)
    if not r:
        raise HTTPException(404, "审批申请不存在")
    # 员工只能查自己的
    if user.role == Role.EMPLOYEE and r.requester_id != user.id:
        raise HTTPException(403, "无权查看")
    return _req(r, db)


@router.post("")
def create_approval(
    req: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发起审批申请"""
    r = ApprovalRequest(
        request_type=req.request_type,
        target_id=req.target_id,
        target_type=req.target_type,
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return _req(r, db)


@router.post("/{request_id}/actions")
def act_on_approval(
    request_id: int,
    req: ApprovalActionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """审批动作：approve / reject / add_conditions"""
    r = db.get(ApprovalRequest, request_id)
    if not r:
        raise HTTPException(404, "审批申请不存在")
    if r.status != ApprovalStatus.PENDING:
        raise HTTPException(400, f"审批已完结（状态：{r.status}），不可重复操作")

    action_map = {
        "approve": ApprovalActionType.APPROVE,
        "reject": ApprovalActionType.REJECT,
        "add_conditions": ApprovalActionType.ADD_CONDITIONS,
    }
    action_enum = action_map.get(req.action)
    if not action_enum:
        raise HTTPException(400, f"未知操作：{req.action}")

    a = ApprovalAction(
        request_id=request_id,
        actor_id=user.id,
        action=action_enum,
        comment=req.comment,
    )
    db.add(a)

    # 更新申请状态
    if req.action == "approve":
        r.status = ApprovalStatus.APPROVED
        # 若是 skill 发布审批，自动把 skill 状态改为 published
        if r.request_type == ApprovalRequestType.SKILL_PUBLISH and r.target_type == "skill" and r.target_id:
            from app.models.skill import Skill, SkillStatus
            skill = db.get(Skill, r.target_id)
            if skill:
                skill.status = SkillStatus.PUBLISHED
                from app.routers.skills import _ensure_skill_policy
                _ensure_skill_policy(r.target_id, user, db)
    elif req.action == "reject":
        r.status = ApprovalStatus.REJECTED
    elif req.action == "add_conditions":
        r.status = ApprovalStatus.CONDITIONS
        if req.conditions:
            r.conditions = req.conditions

    db.commit()
    db.refresh(r)
    return _req(r, db)
