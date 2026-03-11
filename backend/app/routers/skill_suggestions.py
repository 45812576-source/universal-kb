"""Skill improvement suggestions API."""
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillSuggestion, SuggestionStatus
from app.models.user import User, Role
from app.models.conversation import Message, Conversation

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
        "reaction_type": s.reaction_type,
        "source_message_id": s.source_message_id,
    }


class SuggestionCreate(BaseModel):
    problem_desc: str
    expected_direction: str
    case_example: str = None


class MessageReact(BaseModel):
    reaction_type: str  # "like" or "comment"
    comment: Optional[str] = None  # text when reaction_type == "comment"


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


# POST /api/messages/{msg_id}/react — react to an assistant message (like or comment)
@router.post("/api/messages/{msg_id}/react")
def react_to_message(
    msg_id: int,
    req: MessageReact,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    msg = db.get(Message, msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg.role.value != "assistant":
        raise HTTPException(400, "Can only react to assistant messages")

    # Find the skill_id from the conversation
    conv = db.get(Conversation, msg.conversation_id)
    skill_id = conv.skill_id if conv else None
    if not skill_id:
        # Try metadata
        meta = msg.metadata_ or {}
        skill_id = meta.get("skill_id")
    if not skill_id:
        raise HTTPException(400, "No skill associated with this message")

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    if req.reaction_type == "like":
        s = SkillSuggestion(
            skill_id=skill_id,
            submitted_by=user.id,
            problem_desc="[点赞]",
            expected_direction="用户对该回复点赞，表示满意",
            reaction_type="like",
            source_message_id=msg_id,
            status=SuggestionStatus.PENDING,
        )
    elif req.reaction_type == "comment":
        if not req.comment:
            raise HTTPException(400, "comment field required for reaction_type=comment")
        s = SkillSuggestion(
            skill_id=skill_id,
            submitted_by=user.id,
            problem_desc=req.comment,
            expected_direction="来自对话消息的用户评论",
            reaction_type="comment",
            source_message_id=msg_id,
            status=SuggestionStatus.PENDING,
        )
    else:
        raise HTTPException(400, "reaction_type must be 'like' or 'comment'")

    db.add(s)
    db.commit()
    db.refresh(s)
    return {"ok": True, "id": s.id, "reaction_type": req.reaction_type}


# GET /api/skills/{skill_id}/comments — list comments (suggestions) for a skill (owner view)
@router.get("/api/skills/{skill_id}/comments")
def list_comments(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    # Only skill owner or admins can see comments
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    is_owner = skill.created_by == user.id
    if not is_admin and not is_owner:
        raise HTTPException(403, "Not authorized")

    suggestions = (
        db.query(SkillSuggestion)
        .filter(SkillSuggestion.skill_id == skill_id)
        .order_by(SkillSuggestion.created_at.desc())
        .all()
    )
    return [_suggestion_detail(s) for s in suggestions]
