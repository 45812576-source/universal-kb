from __future__ import annotations

import datetime
import logging
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app.api_envelope import ApiEnvelopeException
from app.database import SessionLocal
from app.models.skill_governance import SkillGovernanceJob
from app.models.user import User
from app.services.skill_governance_service import (
    assert_skill_governance_access,
    generate_declaration,
    generate_permission_case_plan,
    resolve_workspace_id,
    serialize_case_plan,
    serialize_declaration,
    serialize_bundle,
    suggest_role_asset_policies,
)

logger = logging.getLogger(__name__)

GOVERNANCE_JOB_TERMINAL_STATUSES = {"success", "failed"}


def create_governance_job(
    db: Session,
    *,
    skill_id: int,
    workspace_id: int,
    job_type: str,
    payload: dict[str, Any],
    created_by: int,
) -> SkillGovernanceJob:
    job = SkillGovernanceJob(
        skill_id=skill_id,
        workspace_id=workspace_id,
        job_type=job_type,
        status="queued",
        phase="queued",
        payload_json=payload,
        result_json={},
        created_by=created_by,
    )
    db.add(job)
    db.flush()
    return job


def serialize_governance_job(job: SkillGovernanceJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "skill_id": job.skill_id,
        "job_type": job.job_type,
        "status": job.status,
        "phase": job.phase,
        "payload": job.payload_json or {},
        "result": job.result_json or {},
        "error": {
            "code": job.error_code,
            "message": job.error_message,
            "details": {},
        } if job.error_code or job.error_message else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _http_error_code(exc: HTTPException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"])
    return f"http_{getattr(exc, 'status_code', 500)}"


def _http_error_message(exc: HTTPException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    return str(detail)


def process_governance_job(job_id: int, session_factory: Callable[[], Session] = SessionLocal) -> None:
    db = session_factory()
    try:
        job = db.get(SkillGovernanceJob, job_id)
        if not job or job.status in GOVERNANCE_JOB_TERMINAL_STATUSES:
            return
        job.status = "running"
        job.phase = "running"
        job.started_at = datetime.datetime.utcnow()
        job.error_code = None
        job.error_message = None
        db.commit()

        payload = dict(job.payload_json or {})
        user = db.get(User, job.created_by)
        if not user:
            raise HTTPException(404, "User not found")
        skill = assert_skill_governance_access(db, job.skill_id, user)

        if job.job_type == "role_asset_policy_suggestion":
            job.phase = "generating_policies"
            db.flush()
            bundle = suggest_role_asset_policies(
                db,
                skill,
                user,
                job.workspace_id or resolve_workspace_id(db, job.skill_id),
                payload.get("mode") or "initial",
            )
            job.result_json = {
                "bundle_id": bundle.id,
                "bundle_version": bundle.bundle_version,
                "bundle": serialize_bundle(bundle),
            }
        elif job.job_type == "permission_declaration_generation":
            job.phase = "generating_declaration"
            db.flush()
            declaration = generate_declaration(db, skill, user, payload.get("bundle_id"))
            job.result_json = {
                "declaration_id": declaration.id,
                "declaration": serialize_declaration(declaration),
            }
        elif job.job_type == "permission_case_plan_generation":
            job.phase = "generating_case_plan"
            db.flush()
            plan = generate_permission_case_plan(
                db,
                skill,
                user,
                workspace_id=job.workspace_id or resolve_workspace_id(db, job.skill_id),
                focus_mode=payload.get("focus_mode") or "risk_focused",
                max_cases=int(payload.get("max_cases") or 12),
            )
            # 回写测试流谱系元数据
            if payload.get("generation_mode"):
                plan.generation_mode = payload["generation_mode"]
            if payload.get("source_plan_id"):
                plan.source_plan_id = payload["source_plan_id"]
            if payload.get("entry_source"):
                plan.entry_source = payload["entry_source"]
            if payload.get("conversation_id") is not None:
                plan.conversation_id = payload["conversation_id"]
            job.result_json = {
                "plan_id": plan.id,
                "plan": serialize_case_plan(plan),
            }
        else:
            raise HTTPException(400, f"Unsupported governance job type: {job.job_type}")

        job.status = "success"
        job.phase = "done"
        job.finished_at = datetime.datetime.utcnow()
        db.commit()
    except (ApiEnvelopeException, HTTPException) as exc:
        db.rollback()
        failed = db.get(SkillGovernanceJob, job_id)
        if failed:
            failed.status = "failed"
            failed.phase = "failed"
            failed.error_code = _http_error_code(exc)
            failed.error_message = _http_error_message(exc)
            failed.finished_at = datetime.datetime.utcnow()
            db.commit()
    except Exception as exc:
        logger.exception("Skill governance job %s failed", job_id)
        db.rollback()
        failed = db.get(SkillGovernanceJob, job_id)
        if failed:
            failed.status = "failed"
            failed.phase = "failed"
            failed.error_code = "governance.job_failed"
            failed.error_message = str(exc)
            failed.finished_at = datetime.datetime.utcnow()
            db.commit()
    finally:
        db.close()


def process_queued_governance_jobs(limit: int = 10) -> int:
    db = SessionLocal()
    try:
        jobs = (
            db.query(SkillGovernanceJob)
            .filter(SkillGovernanceJob.status == "queued")
            .order_by(SkillGovernanceJob.created_at)
            .limit(limit)
            .all()
        )
        ids = [job.id for job in jobs]
    finally:
        db.close()
    for job_id in ids:
        process_governance_job(job_id)
    return len(ids)


def session_factory_for_current_bind(db: Session) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=db.get_bind())
