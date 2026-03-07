"""Skill improvement suggestions API."""
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillSuggestion, SuggestionStatus
from app.models.user import User, Role

router = APIRouter(tags=["skill-suggestions"])


def _suggestion_detail(s: SkillSuggestion) -> dict:
    return {
        "id": s.id,
        "skill_id": s.skill_id,
        "submitted_by": s.submitted_by,
        "submitter_name": s.submitter.display_name if s.submitter else None,
        "problem_desc": s.problem_desc,
        "expected_direction": s.expected_direction,
        "case_example": s.case_example,
        "status": s.status.value if s.status else "pending",
        "review_note": s.review_note,
        "reviewed_by": s.reviewed_by,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


class SuggestionCreate(BaseModel):
    problem_desc: str
    expected_direction: str
    case_example: str = None


class SuggestionReview(BaseModel):
    status: str  # adopted / partial / rejected
    review_note: str = None


# POST /api/skills/{skill_id}/suggestions
@router.post("/api/skills/{skill_id}/suggestions")
def create_suggestion(
    skill_id: int,
    req: SuggestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    s = SkillSuggestion(
        skill_id=skill_id,
        submitted_by=user.id,
        problem_desc=req.problem_desc,
        expected_direction=req.expected_direction,
        case_example=req.case_example,
        status=SuggestionStatus.PENDING,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "ok": True}


# GET /api/skills/{skill_id}/suggestions
@router.get("/api/skills/{skill_id}/suggestions")
def list_suggestions(
    skill_id: int,
    status: str = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    q = db.query(SkillSuggestion).filter(SkillSuggestion.skill_id == skill_id)
    if status:
        q = q.filter(SkillSuggestion.status == status)
    suggestions = q.order_by(SkillSuggestion.created_at.desc()).all()
    return [_suggestion_detail(s) for s in suggestions]


# PATCH /api/skill-suggestions/{id}/review
@router.patch("/api/skill-suggestions/{suggestion_id}/review")
def review_suggestion(
    suggestion_id: int,
    req: SuggestionReview,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    valid = {s.value for s in SuggestionStatus} - {"pending"}
    if req.status not in valid:
        raise HTTPException(400, f"Invalid status: {req.status}. Must be one of {valid}")

    s = db.get(SkillSuggestion, suggestion_id)
    if not s:
        raise HTTPException(404, "Suggestion not found")

    s.status = req.status
    s.review_note = req.review_note
    s.reviewed_by = user.id
    s.reviewed_at = datetime.datetime.utcnow()
    db.commit()
    return {"ok": True, "id": suggestion_id, "status": req.status}


# GET /api/my/suggestions
@router.get("/api/my/suggestions")
def my_suggestions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    suggestions = (
        db.query(SkillSuggestion)
        .filter(SkillSuggestion.submitted_by == user.id)
        .order_by(SkillSuggestion.created_at.desc())
        .all()
    )
    return [_suggestion_detail(s) for s in suggestions]
