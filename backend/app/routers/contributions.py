"""Contribution statistics API."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import SkillAttribution, SkillSuggestion, SuggestionStatus, AttributionLevel
from app.models.user import Department, Role, User

router = APIRouter(prefix="/api/contributions", tags=["contributions"])


@router.get("/stats")
def contribution_stats(
    department_id: int = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Per-user contribution stats: submission count, adoption rate, influence score, skill count."""
    # Get all users (filtered by dept if requested)
    user_q = db.query(User)
    if department_id:
        user_q = user_q.filter(User.department_id == department_id)
    users = user_q.all()
    user_ids = [u.id for u in users]

    if not user_ids:
        return []

    # Suggestion counts per user
    suggestion_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillSuggestion.id).label("total"),
        )
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    suggestion_map = {r.submitted_by: r.total for r in suggestion_rows}

    # Adopted/partial counts per user
    adopted_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillSuggestion.id).label("adopted"),
        )
        .filter(
            SkillSuggestion.submitted_by.in_(user_ids),
            SkillSuggestion.status.in_([SuggestionStatus.ADOPTED, SuggestionStatus.PARTIAL]),
        )
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    adopted_map = {r.submitted_by: r.adopted for r in adopted_rows}

    # Attribution influence scores (full×3 + partial×1)
    # Join suggestion → attribution to get per-user attribution
    attr_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            SkillAttribution.attribution_level,
            func.count(SkillAttribution.id).label("cnt"),
            func.count(func.distinct(SkillAttribution.skill_id)).label("skill_count"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .group_by(SkillSuggestion.submitted_by, SkillAttribution.attribution_level)
        .all()
    )

    score_map: dict[int, int] = {}
    skill_count_map: dict[int, set] = {}
    for r in attr_rows:
        uid = r.submitted_by
        if r.attribution_level == AttributionLevel.FULL:
            score_map[uid] = score_map.get(uid, 0) + r.cnt * 3
        elif r.attribution_level == AttributionLevel.PARTIAL:
            score_map[uid] = score_map.get(uid, 0) + r.cnt * 1
        if uid not in skill_count_map:
            skill_count_map[uid] = set()

    # Get distinct skill counts per user
    skill_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(func.distinct(SkillAttribution.skill_id)).label("skill_count"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .filter(SkillAttribution.attribution_level != AttributionLevel.NONE)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    skill_count_final = {r.submitted_by: r.skill_count for r in skill_rows}

    result = []
    for u in users:
        total = suggestion_map.get(u.id, 0)
        adopted = adopted_map.get(u.id, 0)
        score = score_map.get(u.id, 0)
        skills = skill_count_final.get(u.id, 0)
        result.append({
            "user_id": u.id,
            "display_name": u.display_name,
            "department_id": u.department_id,
            "total_suggestions": total,
            "adopted_count": adopted,
            "adoption_rate": round(adopted / total, 2) if total > 0 else 0.0,
            "influence_score": score,
            "impacted_skills": skills,
        })

    # Sort by influence score desc
    result.sort(key=lambda x: (-x["influence_score"], -x["total_suggestions"]))
    return result


@router.get("/leaderboard")
def leaderboard(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Top contributors leaderboard — visible to all logged-in users."""
    all_stats = contribution_stats.__wrapped__(department_id=None, db=db, _user=_user) \
        if hasattr(contribution_stats, "__wrapped__") else []

    # Simpler direct query for leaderboard
    attr_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillAttribution.id).label("full_cnt"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillAttribution.attribution_level == AttributionLevel.FULL)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    partial_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillAttribution.id).label("partial_cnt"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillAttribution.attribution_level == AttributionLevel.PARTIAL)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    full_map = {r.submitted_by: r.full_cnt for r in attr_rows}
    partial_map = {r.submitted_by: r.partial_cnt for r in partial_rows}
    user_ids = set(full_map.keys()) | set(partial_map.keys())

    if not user_ids:
        return []

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u for u in users}
    dept_map = {d.id: d.name for d in db.query(Department).all()}

    entries = []
    for uid in user_ids:
        score = full_map.get(uid, 0) * 3 + partial_map.get(uid, 0)
        u = user_map.get(uid)
        if not u:
            continue
        entries.append({
            "user_id": uid,
            "display_name": u.display_name,
            "department": dept_map.get(u.department_id, "") if u.department_id else "",
            "influence_score": score,
        })

    entries.sort(key=lambda x: -x["influence_score"])
    return entries[:limit]
