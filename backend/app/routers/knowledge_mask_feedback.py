"""脱敏纠错反馈 CRUD 接口。"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
from app.models.skill_knowledge_ref import KnowledgeMaskFeedback, KnowledgeMaskRuleVersion
from app.models.user import Role, User
from app.models.permission import PermissionAuditLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge/mask-feedback", tags=["knowledge-mask-feedback"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    knowledge_id: int
    suggested_desensitization_level: str
    suggested_data_type_adjustments: list[dict] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    evidence_snippet: Optional[str] = None


class FeedbackReview(BaseModel):
    review_note: Optional[str] = None
    review_action: str = Field(default="update_file")  # update_file / update_rule


# ── 提交纠错 ─────────────────────────────────────────────────────────────────

@router.post("")
def submit_feedback(
    body: FeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, body.knowledge_id)
    if not entry:
        raise HTTPException(404, "知识条目不存在")

    profile = db.query(KnowledgeUnderstandingProfile).filter(
        KnowledgeUnderstandingProfile.knowledge_id == body.knowledge_id
    ).first()

    feedback = KnowledgeMaskFeedback(
        knowledge_id=body.knowledge_id,
        understanding_profile_id=profile.id if profile else None,
        submitted_by=user.id,
        current_desensitization_level=profile.desensitization_level if profile else None,
        current_data_type_hits=profile.data_type_hits if profile else [],
        suggested_desensitization_level=body.suggested_desensitization_level,
        suggested_data_type_adjustments=body.suggested_data_type_adjustments,
        reason=body.reason,
        evidence_snippet=body.evidence_snippet,
    )
    db.add(feedback)

    # 标记 profile 为待纠错
    if profile:
        profile.correction_status = "pending_correction"

    db.commit()
    db.refresh(feedback)
    return {"id": feedback.id, "status": "pending", "message": "建议已提交，等待管理员审核"}


# ── 列表查询 ─────────────────────────────────────────────────────────────────

@router.get("")
def list_feedbacks(
    status: Optional[str] = Query(None),
    knowledge_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(KnowledgeMaskFeedback)

    # 非超管只看自己提交的
    if user.role != Role.SUPER_ADMIN:
        q = q.filter(KnowledgeMaskFeedback.submitted_by == user.id)

    if status:
        q = q.filter(KnowledgeMaskFeedback.status == status)
    if knowledge_id:
        q = q.filter(KnowledgeMaskFeedback.knowledge_id == knowledge_id)

    total = q.count()
    items = q.order_by(KnowledgeMaskFeedback.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": fb.id,
                "knowledge_id": fb.knowledge_id,
                "knowledge_title": fb.knowledge.title if fb.knowledge else None,
                "submitted_by": fb.submitted_by,
                "submitter_name": fb.submitter.display_name if fb.submitter else None,
                "current_desensitization_level": fb.current_desensitization_level,
                "current_data_type_hits": fb.current_data_type_hits,
                "suggested_desensitization_level": fb.suggested_desensitization_level,
                "suggested_data_type_adjustments": fb.suggested_data_type_adjustments,
                "reason": fb.reason,
                "evidence_snippet": fb.evidence_snippet,
                "status": fb.status,
                "reviewed_by": fb.reviewed_by,
                "reviewer_name": fb.reviewer.display_name if fb.reviewer else None,
                "review_note": fb.review_note,
                "review_action": fb.review_action,
                "reviewed_at": fb.reviewed_at.isoformat() if fb.reviewed_at else None,
                "created_at": fb.created_at.isoformat() if fb.created_at else None,
            }
            for fb in items
        ],
    }


# ── 超管批准 ─────────────────────────────────────────────────────────────────

@router.post("/{feedback_id}/approve")
def approve_feedback(
    feedback_id: int,
    body: FeedbackReview,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    fb = db.get(KnowledgeMaskFeedback, feedback_id)
    if not fb:
        raise HTTPException(404, "纠错建议不存在")
    if fb.status != "pending":
        raise HTTPException(400, f"当前状态为 {fb.status}，无法审批")

    fb.status = "approved"
    fb.reviewed_by = user.id
    fb.reviewed_at = datetime.datetime.utcnow()
    fb.review_note = body.review_note
    fb.review_action = body.review_action

    # 动作：update_file — 更新当前文件的 KnowledgeUnderstandingProfile
    if body.review_action == "update_file":
        profile = db.query(KnowledgeUnderstandingProfile).filter(
            KnowledgeUnderstandingProfile.knowledge_id == fb.knowledge_id
        ).first()
        if profile:
            old_level = profile.desensitization_level
            profile.desensitization_level = fb.suggested_desensitization_level
            profile.correction_status = "corrected"
            # 审计日志
            db.add(PermissionAuditLog(
                operator_id=user.id,
                action="mask_feedback_approve_update_file",
                target_table="knowledge_understanding_profiles",
                target_id=profile.id,
                old_values={"desensitization_level": old_level},
                new_values={"desensitization_level": fb.suggested_desensitization_level},
                reason=f"纠错反馈 #{fb.id}: {fb.reason}",
            ))

    # 动作：update_rule — 记录到 KnowledgeMaskRuleVersion
    elif body.review_action == "update_rule":
        # 获取当前最大版本号
        max_ver = db.query(KnowledgeMaskRuleVersion.version).order_by(
            KnowledgeMaskRuleVersion.version.desc()
        ).first()
        new_ver = (max_ver[0] + 1) if max_ver else 1

        rule_version = KnowledgeMaskRuleVersion(
            version=new_ver,
            changes=[{
                "feedback_id": fb.id,
                "change_type": "level_adjustment",
                "before": fb.current_desensitization_level,
                "after": fb.suggested_desensitization_level,
            }],
            approved_by=user.id,
        )
        db.add(rule_version)
        db.add(PermissionAuditLog(
            operator_id=user.id,
            action="mask_feedback_approve_update_rule",
            target_table="knowledge_mask_rule_versions",
            target_id=0,
            old_values={},
            new_values={"version": new_ver, "feedback_id": fb.id},
            reason=f"纠错反馈 #{fb.id}: {fb.reason}",
        ))

    db.commit()
    return {"id": fb.id, "status": "approved", "review_action": body.review_action}


# ── 超管驳回 ─────────────────────────────────────────────────────────────────

@router.post("/{feedback_id}/reject")
def reject_feedback(
    feedback_id: int,
    body: FeedbackReview,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    fb = db.get(KnowledgeMaskFeedback, feedback_id)
    if not fb:
        raise HTTPException(404, "纠错建议不存在")
    if fb.status != "pending":
        raise HTTPException(400, f"当前状态为 {fb.status}，无法驳回")

    fb.status = "rejected"
    fb.reviewed_by = user.id
    fb.reviewed_at = datetime.datetime.utcnow()
    fb.review_note = body.review_note

    # 清除待纠错标记
    profile = db.query(KnowledgeUnderstandingProfile).filter(
        KnowledgeUnderstandingProfile.knowledge_id == fb.knowledge_id
    ).first()
    if profile and profile.correction_status == "pending_correction":
        profile.correction_status = None

    db.commit()
    return {"id": fb.id, "status": "rejected"}
