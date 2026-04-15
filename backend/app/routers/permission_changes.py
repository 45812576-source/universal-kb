"""权限变更请求工作流

前端调用:
  GET   /api/admin/permission-changes?target_user_id=&status=
  POST  /api/admin/permission-changes               (创建变更请求)
  POST  /api/admin/permission-changes/{id}/approve   (审批通过)
  POST  /api/admin/permission-changes/{id}/reject    (审批拒绝)

与审批流集成:
  创建变更请求时自动生成 approval_requests 记录（type=permission_change），
  通过审批页面统一展示。两侧 approve/reject 双向同步。
"""
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge_permission import (
    KnowledgePermissionGrant,
    PermissionChangeDomain,
    PermissionChangeRequest,
    PermissionChangeStatus,
)
from app.models.permission import (
    ApprovalRequest,
    ApprovalRequestType,
    ApprovalStatus,
)
from app.models.user import Role, User

router = APIRouter(prefix="/api/admin", tags=["permission-changes"])


# ─── 高风险 feature flags（与前端 constants.ts 保持一致）───────────────────────

HIGH_RISK_FEATURE_FLAGS = {
    "dev_studio",
    "webapp_publish",
    "feishu_sync",
    "skill_studio_dual_lane_enabled",
    "skill_studio_fast_lane_enabled",
    "skill_studio_deep_lane_enabled",
    "skill_studio_sla_degrade_enabled",
    "skill_studio_patch_protocol_enabled",
    "skill_studio_frontend_run_protocol_enabled",
}

_DEFAULT_FEATURE_FLAGS = {
    "dev_studio": True,
    "asr": True,
    "webapp_publish": True,
    "batch_upload_skill": False,
    "feishu_sync": False,
    "skill_studio_dual_lane_enabled": True,
    "skill_studio_fast_lane_enabled": True,
    "skill_studio_deep_lane_enabled": True,
    "skill_studio_sla_degrade_enabled": True,
    "skill_studio_patch_protocol_enabled": True,
    "skill_studio_frontend_run_protocol_enabled": True,
}


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class CreateChangeRequest(BaseModel):
    target_user_id: int
    domain: str           # "feature_flag" | "model_grant" | "capability_grant"
    action_key: str       # e.g. "dev_studio", "skill.publish.approve_final"
    current_value: Optional[object] = None
    target_value: Optional[object] = None
    reason: Optional[str] = None
    risk_note: Optional[str] = None


class ReviewRequest(BaseModel):
    comment: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_change(c: PermissionChangeRequest) -> dict:
    return {
        "id": c.id,
        "target_user_id": c.target_user_id,
        "domain": c.domain.value if c.domain else c.domain,
        "action_key": c.action_key,
        "current_value": c.current_value,
        "target_value": c.target_value,
        "reason": c.reason,
        "risk_note": c.risk_note,
        "requester_id": c.requester_id,
        "status": c.status.value if c.status else c.status,
        "reviewer_id": c.reviewer_id,
        "review_comment": c.review_comment,
        "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _apply_feature_flag_change(change: PermissionChangeRequest, db: Session):
    """审批通过后实际写入 feature_flags。"""
    user = db.get(User, change.target_user_id)
    if not user:
        return
    flags = dict(user.feature_flags or {})
    # target_value is stored as string; convert "true"/"false" to bool
    val = change.target_value
    if isinstance(val, str):
        val = val.lower() not in ("false", "0", "")
    flags[change.action_key] = val
    user.feature_flags = flags
    db.flush()


def _apply_knowledge_capability_change(change: PermissionChangeRequest, db: Session, reviewer_id: int):
    """审批通过后授予知识资产/审批能力权限。"""
    tv = change.target_value
    if not isinstance(tv, dict):
        return
    grant = KnowledgePermissionGrant(
        grantee_user_id=change.target_user_id,
        resource_type=tv.get("resource_type", "approval_capability"),
        resource_id=tv.get("resource_id"),
        action=tv.get("action", change.action_key),
        scope=tv.get("scope", "exact"),
        source="approval",
        granted_by=reviewer_id,
    )
    db.add(grant)
    db.flush()


def _find_linked_approval(change_id: int, db: Session) -> ApprovalRequest | None:
    """查找与 PermissionChangeRequest 关联的 ApprovalRequest。"""
    return (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.request_type == ApprovalRequestType.PERMISSION_CHANGE,
            ApprovalRequest.target_id == change_id,
            ApprovalRequest.target_type == "permission_change",
        )
        .first()
    )


def apply_permission_change(change: PermissionChangeRequest, db: Session, reviewer_id: int):
    """执行权限变更（供 approvals.py 和本模块共用）。"""
    domain = change.domain
    if domain == PermissionChangeDomain.FEATURE_FLAG:
        _apply_feature_flag_change(change, db)
    elif domain == PermissionChangeDomain.CAPABILITY_GRANT:
        _apply_knowledge_capability_change(change, db, reviewer_id)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/permission-changes")
def list_permission_changes(
    target_user_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """查询权限变更请求列表。"""
    q = db.query(PermissionChangeRequest)
    if target_user_id is not None:
        q = q.filter(PermissionChangeRequest.target_user_id == target_user_id)
    if status:
        q = q.filter(PermissionChangeRequest.status == status)
    q = q.order_by(PermissionChangeRequest.created_at.desc())
    return [_serialize_change(c) for c in q.all()]


@router.post("/permission-changes")
def create_permission_change(
    body: CreateChangeRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """创建权限变更请求，同时生成审批工单。"""
    target = db.get(User, body.target_user_id)
    if not target:
        raise HTTPException(404, "目标用户不存在")

    # 检查是否已有 pending 请求
    existing = (
        db.query(PermissionChangeRequest)
        .filter(
            PermissionChangeRequest.target_user_id == body.target_user_id,
            PermissionChangeRequest.domain == body.domain,
            PermissionChangeRequest.action_key == body.action_key,
            PermissionChangeRequest.status == PermissionChangeStatus.PENDING,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "该用户已有一条待审批的同类变更请求")

    change = PermissionChangeRequest(
        target_user_id=body.target_user_id,
        domain=body.domain,
        action_key=body.action_key,
        current_value=body.current_value,
        target_value=body.target_value,
        reason=body.reason,
        risk_note=body.risk_note,
        requester_id=current.id,
    )
    db.add(change)
    db.flush()  # 获取 change.id

    # 同步创建 ApprovalRequest，让审批页面能看到
    evidence_pack = {
        "domain": body.domain,
        "action_key": body.action_key,
        "current_value": body.current_value,
        "target_value": body.target_value,
        "reason": body.reason,
        "risk_note": body.risk_note,
        "target_user_id": body.target_user_id,
        "target_user_name": target.display_name,
    }
    approval = ApprovalRequest(
        request_type=ApprovalRequestType.PERMISSION_CHANGE,
        target_id=change.id,
        target_type="permission_change",
        requester_id=current.id,
        status=ApprovalStatus.PENDING,
        stage="super_pending",
        conditions=[{"reason": body.reason}] if body.reason else None,
        evidence_pack=evidence_pack,
        risk_level="high",
        impact_summary=body.risk_note,
    )
    db.add(approval)
    db.commit()
    db.refresh(change)
    return _serialize_change(change)


@router.post("/permission-changes/{change_id}/approve")
def approve_permission_change(
    change_id: int,
    body: ReviewRequest = ReviewRequest(),
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """审批通过权限变更请求并执行实际变更。同步更新关联的 ApprovalRequest。"""
    change = db.get(PermissionChangeRequest, change_id)
    if not change:
        raise HTTPException(404, "变更请求不存在")
    if change.status != PermissionChangeStatus.PENDING:
        raise HTTPException(400, "该请求已被处理")

    change.status = PermissionChangeStatus.APPROVED
    change.reviewer_id = current.id
    change.review_comment = body.comment
    change.reviewed_at = datetime.datetime.utcnow()

    apply_permission_change(change, db, current.id)

    # 同步关联的 ApprovalRequest
    linked = _find_linked_approval(change_id, db)
    if linked and linked.status == ApprovalStatus.PENDING:
        linked.status = ApprovalStatus.APPROVED

    db.commit()
    db.refresh(change)
    return _serialize_change(change)


@router.post("/permission-changes/{change_id}/reject")
def reject_permission_change(
    change_id: int,
    body: ReviewRequest = ReviewRequest(),
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """拒绝权限变更请求。同步更新关联的 ApprovalRequest。"""
    change = db.get(PermissionChangeRequest, change_id)
    if not change:
        raise HTTPException(404, "变更请求不存在")
    if change.status != PermissionChangeStatus.PENDING:
        raise HTTPException(400, "该请求已被处理")

    change.status = PermissionChangeStatus.REJECTED
    change.reviewer_id = current.id
    change.review_comment = body.comment
    change.reviewed_at = datetime.datetime.utcnow()

    # 同步关联的 ApprovalRequest
    linked = _find_linked_approval(change_id, db)
    if linked and linked.status == ApprovalStatus.PENDING:
        linked.status = ApprovalStatus.REJECTED

    db.commit()
    db.refresh(change)
    return _serialize_change(change)
