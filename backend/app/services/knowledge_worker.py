"""后台 Worker：周期扫描 knowledge_jobs 表，执行 render 和 classify 任务。

基于 APScheduler BackgroundScheduler，每 30 秒扫一次 queued 的 job。
"""
from __future__ import annotations

import asyncio
import datetime
import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_job import KnowledgeJob

logger = logging.getLogger(__name__)

# 每批最多处理的 job 数
_BATCH_SIZE = 10

# 分类置信度低于此阈值标记为 needs_review
_NEEDS_REVIEW_THRESHOLD = 0.5


def _run_render_job(db: Session, job: KnowledgeJob, entry: KnowledgeEntry) -> None:
    """执行单个 render job。"""
    from app.services.doc_renderer import render_entry

    result = render_entry(db, entry.id)
    if result.get("ok"):
        job.status = "success"
    else:
        job.error_type = "render_error"
        job.error_message = result.get("error", "unknown")[:500]
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"  # 还有重试机会，放回队列


def _run_understand_job(db: Session, job: KnowledgeJob, entry: KnowledgeEntry) -> None:
    """执行单个 understand job。"""
    from app.services.knowledge_understanding import understand_document

    try:
        loop = asyncio.new_event_loop()
        profile = loop.run_until_complete(
            understand_document(
                knowledge_id=entry.id,
                content=entry.content or "",
                filename=entry.source_file or entry.title or "",
                file_type=entry.file_type or "",
                db=db,
            )
        )
        loop.close()
    except Exception as e:
        logger.warning(f"[KnowledgeWorker] understand job {job.id} error: {e}")
        job.error_type = "understand_error"
        job.error_message = str(e)[:500]
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"
        return

    if profile and profile.understanding_status in ("success", "partial"):
        # 向后兼容：同步更新主表
        if profile.display_title:
            entry.ai_title = profile.display_title
        if profile.summary_short:
            entry.ai_summary = profile.summary_short
        job.status = "success"
    else:
        job.error_type = "understand_incomplete"
        job.error_message = getattr(profile, "understanding_error", "")[:500] if profile else "no profile"
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"


def _run_classify_job(db: Session, job: KnowledgeJob, entry: KnowledgeEntry) -> None:
    """执行单个 classify job。"""
    from app.services.knowledge_classifier import classify, apply_classification_to_entry

    try:
        loop = asyncio.new_event_loop()
        cls_result = loop.run_until_complete(classify(entry.content or "", db))
        loop.close()
    except Exception as e:
        logger.warning(f"[KnowledgeWorker] classify job {job.id} error: {e}")
        job.error_type = "classify_error"
        job.error_message = str(e)[:500]
        entry.classification_status = "failed"
        entry.classification_error = str(e)[:500]
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"
        return

    if cls_result:
        apply_classification_to_entry(entry, cls_result)
        entry.classification_source = cls_result.stage
        entry.classified_at = datetime.datetime.utcnow()

        if cls_result.confidence < _NEEDS_REVIEW_THRESHOLD:
            entry.classification_status = "needs_review"
        else:
            entry.classification_status = "success"
        entry.classification_error = None
        job.status = "success"
    else:
        # 无法分类
        entry.classification_status = "failed"
        entry.classification_error = "分类器未返回结果"
        job.error_type = "no_result"
        job.error_message = "分类器未返回结果"
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"


def process_knowledge_jobs():
    """扫描并执行一批 queued 的 knowledge jobs。由 scheduler 周期调用。"""
    db = SessionLocal()
    try:
        jobs = (
            db.query(KnowledgeJob)
            .filter(KnowledgeJob.status == "queued")
            .order_by(KnowledgeJob.created_at)
            .limit(_BATCH_SIZE)
            .all()
        )
        if not jobs:
            return

        logger.info(f"[KnowledgeWorker] processing {len(jobs)} jobs")

        for job in jobs:
            entry = db.get(KnowledgeEntry, job.knowledge_id)
            if not entry:
                job.status = "failed"
                job.error_message = "knowledge entry not found"
                db.commit()
                continue

            job.status = "running"
            job.attempt_count += 1
            job.started_at = datetime.datetime.utcnow()
            db.commit()

            try:
                if job.job_type == "render":
                    _run_render_job(db, job, entry)
                elif job.job_type == "classify":
                    _run_classify_job(db, job, entry)
                elif job.job_type == "understand":
                    _run_understand_job(db, job, entry)
                else:
                    job.status = "failed"
                    job.error_message = f"unknown job_type: {job.job_type}"
            except Exception as e:
                logger.exception(f"[KnowledgeWorker] job {job.id} unexpected error")
                job.status = "failed" if job.attempt_count >= job.max_attempts else "queued"
                job.error_message = str(e)[:500]

            job.finished_at = datetime.datetime.utcnow()
            db.commit()
    except Exception:
        logger.exception("[KnowledgeWorker] batch processing error")
    finally:
        db.close()


def backfill_unclassified():
    """补偿任务：扫描未分类/分类失败的知识条目，为其创建 classify job。

    由 scheduler 低频调用（如每 10 分钟一次）。
    """
    db = SessionLocal()
    try:
        from sqlalchemy import or_, and_

        # 找到需要补分类的条目（无 taxonomy_code 且没有 queued/running 的 classify job）
        subq = (
            db.query(KnowledgeJob.knowledge_id)
            .filter(
                KnowledgeJob.job_type == "classify",
                KnowledgeJob.status.in_(["queued", "running"]),
            )
            .subquery()
        )

        entries = (
            db.query(KnowledgeEntry.id)
            .filter(
                or_(
                    KnowledgeEntry.classification_status.in_(["pending", "failed"]),
                    and_(
                        KnowledgeEntry.taxonomy_code.is_(None),
                        KnowledgeEntry.classification_status.is_(None),
                    ),
                ),
                KnowledgeEntry.content.isnot(None),
                KnowledgeEntry.id.notin_(subq),
            )
            .limit(20)
            .all()
        )

        if entries:
            logger.info(f"[KnowledgeWorker] backfill: creating classify jobs for {len(entries)} entries")
            for (eid,) in entries:
                job = KnowledgeJob(
                    knowledge_id=eid,
                    job_type="classify",
                    trigger_source="scheduled",
                )
                db.add(job)
            db.commit()
    except Exception:
        logger.exception("[KnowledgeWorker] backfill error")
    finally:
        db.close()


def backfill_ununderstood():
    """补偿任务：扫描无 understanding profile 的知识条目，创建 understand job。"""
    db = SessionLocal()
    try:
        from sqlalchemy import and_
        from app.models.knowledge_understanding import KnowledgeUnderstandingProfile

        # 已有 queued/running understand job 的条目
        subq = (
            db.query(KnowledgeJob.knowledge_id)
            .filter(
                KnowledgeJob.job_type == "understand",
                KnowledgeJob.status.in_(["queued", "running"]),
            )
            .subquery()
        )

        # 已有 profile 的条目
        profile_subq = (
            db.query(KnowledgeUnderstandingProfile.knowledge_id)
            .subquery()
        )

        entries = (
            db.query(KnowledgeEntry.id)
            .filter(
                KnowledgeEntry.content.isnot(None),
                KnowledgeEntry.id.notin_(subq),
                KnowledgeEntry.id.notin_(profile_subq),
            )
            .limit(20)
            .all()
        )

        if entries:
            logger.info(f"[KnowledgeWorker] backfill: creating understand jobs for {len(entries)} entries")
            for (eid,) in entries:
                job = KnowledgeJob(
                    knowledge_id=eid,
                    job_type="understand",
                    trigger_source="scheduled",
                )
                db.add(job)
            db.commit()
    except Exception:
        logger.exception("[KnowledgeWorker] understand backfill error")
    finally:
        db.close()


def backfill_failed_renders():
    """补偿任务：扫描渲染失败的知识条目，为其创建 render retry job。"""
    db = SessionLocal()
    try:
        subq = (
            db.query(KnowledgeJob.knowledge_id)
            .filter(
                KnowledgeJob.job_type == "render",
                KnowledgeJob.status.in_(["queued", "running"]),
            )
            .subquery()
        )

        entries = (
            db.query(KnowledgeEntry.id)
            .filter(
                KnowledgeEntry.doc_render_status.in_(["failed", "pending"]),
                KnowledgeEntry.oss_key.isnot(None),
                KnowledgeEntry.id.notin_(subq),
            )
            .limit(10)
            .all()
        )

        if entries:
            logger.info(f"[KnowledgeWorker] backfill: creating render jobs for {len(entries)} entries")
            for (eid,) in entries:
                job = KnowledgeJob(
                    knowledge_id=eid,
                    job_type="render",
                    trigger_source="scheduled",
                )
                db.add(job)
            db.commit()
    except Exception:
        logger.exception("[KnowledgeWorker] render backfill error")
    finally:
        db.close()
