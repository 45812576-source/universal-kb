"""Upstream version checker: scheduled daily check for imported skills."""
from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.mcp import McpSource, SkillUpstreamCheck
from app.models.skill import Skill
from app.services.mcp_client import check_upstream_version

logger = logging.getLogger(__name__)


def compute_text_diff_summary(old: str, new: str) -> str:
    """Return a brief diff summary (added/removed line counts)."""
    old_lines = set(old.splitlines())
    new_lines = set(new.splitlines())
    added = len(new_lines - old_lines)
    removed = len(old_lines - new_lines)
    return f"+{added} 行 / -{removed} 行"


def check_all_imported_skills() -> None:
    """Check upstream versions for all imported skills. Called by scheduler."""
    db: Session = SessionLocal()
    try:
        imported_skills = (
            db.query(Skill)
            .filter(Skill.source_type.in_(["imported"]), Skill.upstream_id.isnot(None))
            .all()
        )
        logger.info(f"Upstream check: {len(imported_skills)} imported skills to check")

        for skill in imported_skills:
            try:
                _check_skill(db, skill)
            except Exception as e:
                logger.warning(f"Upstream check failed for skill {skill.id}: {e}")
    finally:
        db.close()


def _check_skill(db: Session, skill: Skill) -> None:
    source = db.query(McpSource).filter(McpSource.is_active == True).first()
    if not source:
        return

    result = check_upstream_version(source, skill)
    if result.get("error"):
        return

    has_diff = result["has_diff"]
    new_version = result.get("new_version", "")

    check = SkillUpstreamCheck(
        skill_id=skill.id,
        checked_at=datetime.datetime.utcnow(),
        upstream_version=new_version,
        has_diff=has_diff,
        diff_summary=None,
        action="pending",
    )

    if has_diff and result.get("remote"):
        remote_content = result["remote"].get("system_prompt", "")
        if skill.upstream_content and remote_content:
            check.diff_summary = compute_text_diff_summary(skill.upstream_content, remote_content)

    db.add(check)
    db.commit()
    logger.info(f"Skill {skill.id} upstream check: has_diff={has_diff}")
