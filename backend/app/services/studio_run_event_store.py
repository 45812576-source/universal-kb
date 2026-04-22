"""Studio Run Event Store — DB-backed append-only event log.

Phase B2: 每次 event append 同步写 DB，StudioRunRegistry 只做热缓存。
支持 after_sequence replay 以实现断线重连和后端重启恢复。
"""
from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.models.agent_run import AgentRun, AgentRunEvent

logger = logging.getLogger(__name__)


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _new_idempotency_key(public_run_id: str, sequence: int) -> str:
    return f"{public_run_id}:{sequence}"


# ── Run CRUD ──────────────────────────────────────────────────────────────────


def create_run(
    db: DBSession,
    *,
    public_run_id: str,
    conversation_id: int,
    user_id: int,
    skill_id: int | None = None,
    run_version: int = 1,
    parent_run_id: str | None = None,
    active_card_id: str | None = None,
) -> AgentRun:
    """创建一条 agent_runs 记录。"""
    row = AgentRun(
        public_run_id=public_run_id,
        conversation_id=conversation_id,
        user_id=user_id,
        skill_id=skill_id,
        run_version=run_version,
        parent_run_id=parent_run_id,
        active_card_id=active_card_id,
        status="queued",
        started_at=_now(),
        created_at=_now(),
    )
    db.add(row)
    _safe_flush(db)
    return row


def update_run_status(
    db: DBSession,
    public_run_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    superseded_by: str | None = None,
    message_id: int | None = None,
    harness_run_id: str | None = None,
) -> AgentRun | None:
    """更新 run 状态。"""
    row = db.query(AgentRun).filter(AgentRun.public_run_id == public_run_id).first()
    if not row:
        logger.warning("update_run_status: run %s not found in DB", public_run_id)
        return None
    row.status = status
    if error_code is not None:
        row.error_code = error_code
    if error_message is not None:
        row.error_message = error_message
    if superseded_by is not None:
        row.superseded_by = superseded_by
    if message_id is not None:
        row.message_id = message_id
    if harness_run_id is not None:
        row.harness_run_id = harness_run_id
    if status in ("completed", "failed"):
        row.completed_at = _now()
    elif status == "cancelled":
        row.cancelled_at = _now()
    _safe_flush(db)
    return row


def set_harness_run_id(
    db: DBSession,
    public_run_id: str,
    harness_run_id: str,
) -> None:
    """skill_studio profile 创建 HarnessRun 后回写关联。"""
    row = db.query(AgentRun).filter(AgentRun.public_run_id == public_run_id).first()
    if row:
        row.harness_run_id = harness_run_id
        _safe_flush(db)


# ── Event Append ──────────────────────────────────────────────────────────────


def append_event(
    db: DBSession,
    *,
    public_run_id: str,
    run_version: int,
    sequence: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    patch_type: str | None = None,
    harness_run_id: str | None = None,
    idempotency_key: str | None = None,
) -> AgentRunEvent:
    """追加一条 event 到 DB。幂等：相同 idempotency_key 不会重复写入。"""
    idem_key = idempotency_key or _new_idempotency_key(public_run_id, sequence)

    # 幂等检查
    existing = (
        db.query(AgentRunEvent.id)
        .filter(AgentRunEvent.idempotency_key == idem_key)
        .first()
    )
    if existing:
        return db.query(AgentRunEvent).filter(AgentRunEvent.id == existing[0]).first()

    row = AgentRunEvent(
        public_run_id=public_run_id,
        run_version=run_version,
        harness_run_id=harness_run_id,
        sequence=sequence,
        event_type=event_type,
        patch_type=patch_type,
        payload_json=payload or {},
        idempotency_key=idem_key,
        created_at=_now(),
    )
    db.add(row)
    _safe_flush(db)
    return row


# ── Query ─────────────────────────────────────────────────────────────────────


def get_run(db: DBSession, public_run_id: str) -> AgentRun | None:
    return db.query(AgentRun).filter(AgentRun.public_run_id == public_run_id).first()


def get_active_run(
    db: DBSession,
    conversation_id: int,
    *,
    user_id: int | None = None,
    skill_id: int | None = None,
) -> AgentRun | None:
    """查找指定 conversation 的 active run（queued/running/waiting_*）。"""
    active_statuses = {"queued", "running", "waiting_tool", "waiting_user", "waiting_approval"}
    q = (
        db.query(AgentRun)
        .filter(
            AgentRun.conversation_id == conversation_id,
            AgentRun.status.in_(active_statuses),
        )
        .order_by(AgentRun.created_at.desc())
    )
    if user_id is not None:
        q = q.filter(AgentRun.user_id == user_id)
    if skill_id is not None:
        q = q.filter(AgentRun.skill_id == skill_id)
    return q.first()


def get_recent_run(
    db: DBSession,
    conversation_id: int,
    *,
    user_id: int | None = None,
) -> AgentRun | None:
    """获取最近一次 run（任意状态），用于刷新恢复。"""
    q = (
        db.query(AgentRun)
        .filter(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.created_at.desc())
    )
    if user_id is not None:
        q = q.filter(AgentRun.user_id == user_id)
    return q.first()


def get_events_after(
    db: DBSession,
    public_run_id: str,
    after_sequence: int = 0,
    *,
    limit: int = 500,
) -> list[AgentRunEvent]:
    """从 DB 读取指定 run 在 after_sequence 之后的 events，用于断线重连。"""
    return (
        db.query(AgentRunEvent)
        .filter(
            AgentRunEvent.public_run_id == public_run_id,
            AgentRunEvent.sequence > after_sequence,
        )
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
        .all()
    )


def get_all_events(
    db: DBSession,
    public_run_id: str,
) -> list[AgentRunEvent]:
    """获取完整 event log，用于 replay。"""
    return (
        db.query(AgentRunEvent)
        .filter(AgentRunEvent.public_run_id == public_run_id)
        .order_by(AgentRunEvent.sequence.asc())
        .all()
    )


def supersede_active_runs(
    db: DBSession,
    conversation_id: int,
    *,
    superseded_by: str,
    user_id: int | None = None,
) -> list[str]:
    """将指定 conversation 的所有 active runs 标记为 superseded。返回被 supersede 的 run ids。"""
    active_statuses = {"queued", "running", "waiting_tool", "waiting_user", "waiting_approval"}
    q = (
        db.query(AgentRun)
        .filter(
            AgentRun.conversation_id == conversation_id,
            AgentRun.status.in_(active_statuses),
        )
    )
    if user_id is not None:
        q = q.filter(AgentRun.user_id == user_id)

    superseded_ids = []
    for row in q.all():
        row.status = "superseded"
        row.superseded_by = superseded_by
        row.completed_at = _now()
        superseded_ids.append(row.public_run_id)

    if superseded_ids:
        _safe_flush(db)
    return superseded_ids


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_flush(db: DBSession) -> None:
    try:
        db.flush()
    except Exception:
        logger.exception("DB flush failed in event store")
