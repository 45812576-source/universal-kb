"""Task creator tool — creates tasks in the tasks table from AI-generated data.

Input params:
{
  "title": "任务标题",
  "description": "任务描述（可选）",
  "priority": "urgent_important | important | urgent | neither",
  "due_date": "2026-03-10T14:00:00（可选，ISO 8601）",
  "sub_tasks": [                    # 可选，子任务列表
    {"title": "...", "description": "...", "priority": "..."}
  ]
}

Output: {"task_id": 1, "title": "...", "created": true, "message": "..."}
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)


def execute(params: dict, db=None, user_id: int | None = None) -> Any:
    if db is None or user_id is None:
        raise ValueError("task_creator requires db and user_id context")

    from app.models.task import Task, TaskPriority, TaskStatus

    title = params.get("title", "").strip()
    if not title:
        return {"ok": False, "error": "任务标题不能为空"}

    description = params.get("description", "")
    priority_str = params.get("priority", "neither")
    due_date_str = params.get("due_date")
    sub_tasks_data = params.get("sub_tasks", [])

    # Parse priority — accept both lowercase and UPPERCASE from LLM
    priority_map = {
        "urgent_important": TaskPriority.URGENT_IMPORTANT,
        "important": TaskPriority.IMPORTANT,
        "urgent": TaskPriority.URGENT,
        "neither": TaskPriority.NEITHER,
    }
    priority = priority_map.get(priority_str.lower(), TaskPriority.NEITHER)

    # Parse due_date
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
            # strip timezone for MySQL datetime
            due_date = due_date.replace(tzinfo=None)
        except Exception:
            pass

    # Create main task
    task = Task(
        title=title,
        description=description or None,
        priority=priority,
        due_date=due_date,
        assignee_id=user_id,
        created_by_id=user_id,
        source_type="ai_generated",
    )
    db.add(task)
    db.flush()  # get task.id

    created_ids = [task.id]

    # Create sub-tasks
    for sub in sub_tasks_data:
        sub_title = sub.get("title", "").strip()
        if not sub_title:
            continue
        sub_priority = priority_map.get((sub.get("priority") or "neither").lower(), TaskPriority.NEITHER)
        sub_task = Task(
            title=sub_title,
            description=sub.get("description") or None,
            priority=sub_priority,
            assignee_id=user_id,
            created_by_id=user_id,
            source_type="ai_generated",
            source_id=task.id,  # parent task id
        )
        db.add(sub_task)
        created_ids.append(sub_task.id)

    db.commit()

    sub_count = len(created_ids) - 1
    msg = f"已创建任务「{title}」"
    if sub_count:
        msg += f"，含 {sub_count} 个子任务"
    if due_date:
        msg += f"，截止 {due_date.strftime('%m月%d日 %H:%M')}"

    return {
        "task_id": task.id,
        "title": title,
        "priority": priority_str,
        "due_date": due_date_str,
        "sub_task_count": sub_count,
        "created": True,
        "message": msg,
    }
