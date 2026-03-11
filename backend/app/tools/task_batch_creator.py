"""Task batch creation builtin tool.

Input params:
{
  "tasks": [
    {
      "title": "拍摄：美白精华 - 成分科普角度",
      "description": "30s短视频，突出烟酰胺成分...",
      "priority": "important",
      "assignee_name": "小王",       # optional，按 display_name 查 User
      "due_date": "2026-03-15",      # optional YYYY-MM-DD
    },
    ...
  ],
  "source_skill_id": 35,             # optional
  "batch_tag": "W12-选题排期"         # optional
}

Output: {"ok": true, "created_count": 5, "task_ids": [101, 102, ...], "unresolved_assignees": ["小王"]}
"""
from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.models.task import Task, TaskPriority, TaskStatus
from app.models.user import User

logger = logging.getLogger(__name__)

_PRIORITY_MAP = {
    "urgent_important": TaskPriority.URGENT_IMPORTANT,
    "important": TaskPriority.IMPORTANT,
    "urgent": TaskPriority.URGENT,
    "neither": TaskPriority.NEITHER,
}


async def execute(params: dict, db: Session, user_id: int | None = None) -> dict:
    """Batch-create Task records from structured Skill output."""
    tasks_data = params.get("tasks", [])
    source_skill_id = params.get("source_skill_id")
    batch_tag = params.get("batch_tag", "")

    if not tasks_data:
        return {"ok": False, "error": "tasks 列表为空"}

    created_ids = []
    unresolved_assignees = []

    for item in tasks_data:
        # Resolve assignee
        assignee_id = user_id  # default to caller
        assignee_name = item.get("assignee_name")
        if assignee_name and db:
            user = (
                db.query(User)
                .filter(User.display_name.like(f"%{assignee_name}%"))
                .first()
            )
            if user:
                assignee_id = user.id
            else:
                unresolved_assignees.append(assignee_name)

        if not assignee_id:
            logger.warning("task_batch_creator: no user_id available, skipping assignee resolution")
            assignee_id = 1  # fallback to first user

        # Parse due_date
        due_date = None
        if item.get("due_date"):
            try:
                due_date = datetime.datetime.strptime(item["due_date"], "%Y-%m-%d")
            except ValueError:
                pass

        # Parse priority
        priority_str = item.get("priority", "neither")
        priority = _PRIORITY_MAP.get(priority_str, TaskPriority.NEITHER)

        # Build metadata
        meta = {}
        if batch_tag:
            meta["batch_tag"] = batch_tag
        if source_skill_id:
            meta["source_skill_id"] = source_skill_id

        task = Task(
            title=item.get("title", ""),
            description=item.get("description", ""),
            priority=priority,
            status=TaskStatus.PENDING,
            due_date=due_date,
            assignee_id=assignee_id,
            created_by_id=user_id or assignee_id,
            source_type="ai_generated",
            metadata_=meta,
        )
        db.add(task)
        db.flush()
        created_ids.append(task.id)

    db.commit()

    return {
        "ok": True,
        "created_count": len(created_ids),
        "task_ids": created_ids,
        "unresolved_assignees": list(set(unresolved_assignees)),
    }
