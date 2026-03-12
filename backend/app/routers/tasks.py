import datetime
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conversation import Message
from app.models.task import Task, TaskPriority, TaskStatus
from app.models.user import User
from app.services.llm_gateway import llm_gateway

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_dict(task: Task, sub_tasks: Optional[list] = None) -> dict:
    d = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority.value if task.priority else "neither",
        "status": task.status.value if task.status else "pending",
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "assignee_id": task.assignee_id,
        "assignee_name": task.assignee.display_name if task.assignee else None,
        "created_by_id": task.created_by_id,
        "creator_name": task.creator.display_name if task.creator else None,
        "source_type": task.source_type,
        "source_id": task.source_id,
        "conversation_id": task.conversation_id,
        "workspace_id": task.workspace_id,
        "metadata": task.metadata_ or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
    if sub_tasks is not None:
        d["sub_tasks"] = [_task_dict(s) for s in sub_tasks]
    return d


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.NEITHER
    due_date: Optional[datetime.datetime] = None
    assignee_id: Optional[int] = None
    conversation_id: Optional[int] = None
    workspace_id: Optional[int] = None
    source_type: str = "manual"
    source_id: Optional[int] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[TaskPriority] = None
    status: Optional[TaskStatus] = None
    due_date: Optional[datetime.datetime] = None
    assignee_id: Optional[int] = None


class TaskFromMessage(BaseModel):
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.NEITHER
    due_date: Optional[datetime.datetime] = None
    assignee_id: Optional[int] = None


class TaskGenerate(BaseModel):
    description: str
    assignee_id: Optional[int] = None
    auto_execute: bool = False  # True 时走 PEV 三层引擎自动拆解并执行


def _build_task_query(db: Session, user: User, status: Optional[str], priority: Optional[str], parent_only: bool = True):
    now = datetime.datetime.utcnow()
    q = (
        db.query(Task)
        .filter(Task.assignee_id == user.id)
    )
    # Only show top-level tasks; sub_tasks (source_id = parent id) fetched separately
    if parent_only:
        q = q.filter(Task.source_id.is_(None))
    if status:
        q = q.filter(Task.status == status)
    else:
        q = q.filter(Task.status.in_(["pending", "in_progress"]))
    if priority:
        q = q.filter(Task.priority == priority)

    # Eisenhower sort: urgent_important > urgent > important > neither
    # Then: overdue first, then by due_date asc, then created_at desc
    priority_case = case(
        (Task.priority == "urgent_important", 0),
        (Task.priority == "urgent", 1),
        (Task.priority == "important", 2),
        else_=3,
    )
    # MySQL 不支持 NULLS LAST，用 ISNULL() 把 NULL 排到最后
    q = q.order_by(priority_case, func.isnull(Task.due_date), Task.due_date.asc(), Task.created_at.desc())
    return q


@router.get("/users")
def list_users_for_tasks(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List active users for task assignee selection."""
    users = db.query(User).filter(User.is_active == True).order_by(User.display_name).all()
    return [{"id": u.id, "display_name": u.display_name} for u in users]


@router.get("/stats")
def get_task_stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.datetime.utcnow()
    active = (
        db.query(Task)
        .filter(Task.assignee_id == user.id, Task.status.in_(["pending", "in_progress"]), Task.source_id.is_(None))
        .all()
    )
    stats = {
        "urgent_important": 0,
        "important": 0,
        "urgent": 0,
        "neither": 0,
        "overdue": 0,
        "total_pending": len(active),
    }
    for t in active:
        p = t.priority.value if t.priority else "neither"
        if p in stats:
            stats[p] += 1
        if t.due_date and t.due_date < now:
            stats["overdue"] += 1
    return stats


@router.get("")
def list_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parents = _build_task_query(db, user, status, priority, parent_only=True).all()
    parent_ids = [t.id for t in parents]
    # Fetch sub_tasks for all parents in one query
    sub_map: dict[int, list[Task]] = {}
    if parent_ids:
        subs = (
            db.query(Task)
            .filter(Task.source_id.in_(parent_ids), Task.assignee_id == user.id)
            .order_by(Task.created_at)
            .all()
        )
        for s in subs:
            sub_map.setdefault(s.source_id, []).append(s)
    return [_task_dict(t, sub_tasks=sub_map.get(t.id, [])) for t in parents]


@router.post("")
def create_task(
    req: TaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assignee_id = req.assignee_id or user.id
    task = Task(
        title=req.title,
        description=req.description,
        priority=req.priority,
        due_date=req.due_date,
        assignee_id=assignee_id,
        created_by_id=user.id,
        source_type=req.source_type,
        source_id=req.source_id,
        conversation_id=req.conversation_id,
        workspace_id=req.workspace_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_dict(task)


@router.post("/from-message/{msg_id}")
def create_task_from_message(
    msg_id: int,
    req: TaskFromMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    msg = db.get(Message, msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")

    assignee_id = req.assignee_id or user.id
    task = Task(
        title=req.title,
        description=req.description or msg.content[:500],
        priority=req.priority,
        due_date=req.due_date,
        assignee_id=assignee_id,
        created_by_id=user.id,
        source_type="chat_message",
        source_id=msg_id,
        conversation_id=msg.conversation_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_dict(task)


@router.post("/from-draft/{draft_id}")
def create_task_from_draft(
    draft_id: int,
    req: TaskFromMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assignee_id = req.assignee_id or user.id
    task = Task(
        title=req.title,
        description=req.description,
        priority=req.priority,
        due_date=req.due_date,
        assignee_id=assignee_id,
        created_by_id=user.id,
        source_type="draft",
        source_id=draft_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_dict(task)


@router.post("/generate")
async def generate_tasks(
    req: TaskGenerate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """AI拆解：输入描述 → LLM 拆为多个子任务（按艾森豪威尔矩阵标注优先级）。
    auto_execute=True 时走 PEV 三层引擎自动执行。"""
    if req.auto_execute:
        from app.models.pev_job import PEVJob
        from app.services.pev import pev_orchestrator
        import asyncio

        pev_job = PEVJob(
            scenario="task_decomp",
            goal=req.description,
            user_id=user.id,
            config={
                "assignee_id": req.assignee_id or user.id,
                "skip_verify": False,
            },
        )
        db.add(pev_job)
        db.commit()
        db.refresh(pev_job)

        # 后台运行（非阻塞）
        async def _run_pev():
            from app.database import SessionLocal
            bg_db = SessionLocal()
            try:
                bg_job = bg_db.get(PEVJob, pev_job.id)
                async for _ in pev_orchestrator.run(bg_db, bg_job):
                    pass
            finally:
                bg_db.close()

        asyncio.create_task(_run_pev())
        return {"pev_job_id": pev_job.id, "status": "launched", "message": "PEV 任务已启动，正在后台执行"}

    try:
        model_config = llm_gateway.get_config(db)
    except ValueError as e:
        raise HTTPException(500, str(e))

    system_prompt = """你是一个任务拆解助手。用户会提供一段工作描述，你需要将其拆解为具体的可执行子任务。

请按艾森豪威尔矩阵为每个任务标注优先级：
- urgent_important: 重要且紧急（需要立即处理）
- important: 重要不紧急（需要计划安排）
- urgent: 紧急不重要（可以委托他人）
- neither: 不重要不紧急（可以之后再处理）

请以 JSON 数组返回，每个元素包含：
{
  "title": "任务标题（简洁，20字以内）",
  "description": "任务描述（具体说明做什么）",
  "priority": "urgent_important | important | urgent | neither"
}

只返回 JSON 数组，不要其他内容。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.description},
    ]

    try:
        result, _ = await llm_gateway.chat(model_config, messages, temperature=0.3)
        # Strip markdown code blocks if present
        result = result.strip()
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        tasks_data = json.loads(result.strip())
    except Exception as e:
        raise HTTPException(500, f"AI 拆解失败: {str(e)}")

    if not isinstance(tasks_data, list):
        raise HTTPException(500, "AI 返回格式错误")

    return {"tasks": tasks_data}


@router.patch("/{task_id}")
def update_task(
    task_id: int,
    req: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.assignee_id != user.id and task.created_by_id != user.id:
        raise HTTPException(403, "No permission")

    if req.title is not None:
        task.title = req.title
    if req.description is not None:
        task.description = req.description
    if req.priority is not None:
        task.priority = req.priority
    if req.status is not None:
        task.status = req.status
    if req.due_date is not None:
        task.due_date = req.due_date
    if req.assignee_id is not None:
        task.assignee_id = req.assignee_id

    task.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(task)
    return _task_dict(task)


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.created_by_id != user.id:
        raise HTTPException(403, "Only creator can delete")
    db.delete(task)
    db.commit()
    return {"ok": True}
