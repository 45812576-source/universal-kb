import logging

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage
from app.models.user import Role, User
from app.services.review_policy import review_policy
from app.services.vector_service import index_knowledge, delete_knowledge_vectors

logger = logging.getLogger(__name__)


def _do_vectorize(db: Session, entry: KnowledgeEntry) -> None:
    """向量化并写入 Milvus（带分类 metadata，失败不阻塞）。"""
    try:
        milvus_ids = index_knowledge(
            entry.id,
            entry.content,
            created_by=entry.created_by or 0,
            taxonomy_board=entry.taxonomy_board or "",
            taxonomy_code=entry.taxonomy_code or "",
            file_type=entry.file_type or "",
            quality_score=entry.quality_score or 0.5,
        )
        entry.milvus_ids = milvus_ids
    except Exception as e:
        logger.warning(f"Vectorization failed for knowledge {entry.id}: {e}")


def submit_knowledge(db: Session, entry: KnowledgeEntry) -> KnowledgeEntry:
    """统一入库入口：计算审核级别并按策略处理。

    调用方已将 entry 写入 db（add + flush），此函数决定是否直接 APPROVED。
    """
    auto_pass, level, flags, note = review_policy.auto_review(
        capture_mode=entry.capture_mode or "manual_form",
        content=entry.content,
    )

    entry.review_level = level
    entry.sensitivity_flags = flags
    entry.auto_review_note = note

    if auto_pass:
        # L0/L1: 直接收录
        entry.status = KnowledgeStatus.APPROVED
        entry.review_stage = ReviewStage.AUTO_APPROVED
        _do_vectorize(db, entry)
    elif level == 3:
        # L3: 等部门管理员先审核
        entry.review_stage = ReviewStage.PENDING_DEPT
    else:
        # L2: 等部门管理员审核
        entry.review_stage = ReviewStage.PENDING_DEPT

    # L2/L3: 自动创建 knowledge_review 审批单（统一审批流）
    if not auto_pass:
        try:
            from app.models.permission import (
                ApprovalRequest, ApprovalRequestType, ApprovalStatus,
            )
            # Fix 6: 自动采集证据包
            try:
                from app.services.approval_templates import get_auto_evidence
                auto_ep = get_auto_evidence("knowledge_review", "knowledge", entry.id, db)
            except Exception:
                auto_ep = None
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.KNOWLEDGE_REVIEW,
                target_id=entry.id,
                target_type="knowledge",
                requester_id=entry.created_by,
                status=ApprovalStatus.PENDING,
                stage="dept_pending",
                evidence_pack=auto_ep if auto_ep else None,
            )
            db.add(approval)
        except Exception as e:
            logger.warning(
                "Failed to create knowledge_review approval for entry %s: %s",
                entry.id, e,
            )

    db.commit()
    return entry


def approve_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str = ""
) -> KnowledgeEntry:
    """部门管理员审核通过。

    - L2: 直接 APPROVED + 向量化
    - L3: 转为 dept_approved_pending_super，等超管二次确认
    """
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")

    entry.reviewed_by = reviewer_id
    entry.review_note = note

    if (entry.review_level or 2) >= 3:
        # L3: 部门已通过，推给超管
        entry.review_stage = ReviewStage.DEPT_APPROVED_PENDING_SUPER
        # status 仍保持 PENDING，不触发向量化
    else:
        # L2 及以下: 直接通过
        entry.status = KnowledgeStatus.APPROVED
        entry.review_stage = ReviewStage.APPROVED
        _do_vectorize(db, entry)

    db.commit()
    return entry


def reject_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str
) -> KnowledgeEntry:
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")
    entry.status = KnowledgeStatus.REJECTED
    entry.review_stage = ReviewStage.REJECTED
    entry.reviewed_by = reviewer_id
    entry.review_note = note
    db.commit()
    return entry


def super_approve_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str = ""
) -> KnowledgeEntry:
    """超管二次确认通过（仅用于 L3 流程）。"""
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")
    if entry.review_stage != ReviewStage.DEPT_APPROVED_PENDING_SUPER:
        raise ValueError(
            f"Knowledge {knowledge_id} is not in dept_approved_pending_super stage "
            f"(current: {entry.review_stage})"
        )

    entry.status = KnowledgeStatus.APPROVED
    entry.review_stage = ReviewStage.APPROVED
    entry.reviewed_by = reviewer_id
    if note:
        entry.review_note = (entry.review_note or "") + f"\n[超管] {note}"
    _do_vectorize(db, entry)
    db.commit()
    return entry


def super_reject_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str
) -> KnowledgeEntry:
    """超管二次拒绝（仅用于 L3 流程）。"""
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")
    entry.status = KnowledgeStatus.REJECTED
    entry.review_stage = ReviewStage.REJECTED
    entry.reviewed_by = reviewer_id
    entry.review_note = f"[超管拒绝] {note}"
    db.commit()
    return entry
