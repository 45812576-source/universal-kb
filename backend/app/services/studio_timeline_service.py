"""Studio Timeline Service — Fast + Deep 时间线。

Phase B11:
- Fast timeline: 最近 N 个 patch 序列（从热缓存或 DB 最近事件），供前端 SSE 推送
- Deep timeline: 从 agent_run_events 表按 run_id 聚合完整历史，供 debug/eval

API: GET /api/skills/{skill_id}/studio/timeline?mode=fast|deep
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.models.agent_run import AgentRun, AgentRunEvent

logger = logging.getLogger(__name__)

# Fast timeline 默认返回最近 N 个事件
_FAST_LIMIT = 50
# Deep timeline 默认返回最近 N 条 run 记录
_DEEP_RUN_LIMIT = 10


def get_fast_timeline(
    db: DBSession,
    skill_id: int,
    *,
    limit: int = _FAST_LIMIT,
    after_sequence: int = 0,
) -> dict[str, Any]:
    """Fast timeline — 最近 N 个 patch 事件。

    优先从最近一次 active/running run 取事件；
    如无 active run，从最近 completed run 取。
    """
    # 找最近的 run
    recent_run = (
        db.query(AgentRun)
        .filter(AgentRun.skill_id == skill_id)
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    if not recent_run:
        return {"mode": "fast", "events": [], "run_id": None}

    events = (
        db.query(AgentRunEvent)
        .filter(
            AgentRunEvent.public_run_id == recent_run.public_run_id,
            AgentRunEvent.sequence > after_sequence,
        )
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
        .all()
    )

    return {
        "mode": "fast",
        "run_id": recent_run.public_run_id,
        "run_status": recent_run.status,
        "events": [_event_to_dict(e) for e in events],
        "total": len(events),
        "has_more": len(events) >= limit,
    }


def get_deep_timeline(
    db: DBSession,
    skill_id: int,
    *,
    run_limit: int = _DEEP_RUN_LIMIT,
    event_limit_per_run: int = 200,
) -> dict[str, Any]:
    """Deep timeline — 按 run 聚合的完整历史。"""
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.skill_id == skill_id)
        .order_by(AgentRun.created_at.desc())
        .limit(run_limit)
        .all()
    )

    timeline_runs = []
    for run in runs:
        events = (
            db.query(AgentRunEvent)
            .filter(AgentRunEvent.public_run_id == run.public_run_id)
            .order_by(AgentRunEvent.sequence.asc())
            .limit(event_limit_per_run)
            .all()
        )
        timeline_runs.append({
            "run_id": run.public_run_id,
            "harness_run_id": run.harness_run_id,
            "status": run.status,
            "active_card_id": run.active_card_id,
            "run_version": run.run_version,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "event_count": len(events),
            "events": [_event_to_dict(e) for e in events],
        })

    return {
        "mode": "deep",
        "skill_id": skill_id,
        "runs": timeline_runs,
        "total_runs": len(timeline_runs),
    }


def get_run_timeline(
    db: DBSession,
    public_run_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """单个 run 的完整事件序列 — 给 replay/eval 用。"""
    run = db.query(AgentRun).filter(AgentRun.public_run_id == public_run_id).first()
    if not run:
        return {"run_id": public_run_id, "events": [], "error": "run_not_found"}

    events = (
        db.query(AgentRunEvent)
        .filter(
            AgentRunEvent.public_run_id == public_run_id,
            AgentRunEvent.sequence > after_sequence,
        )
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
        .all()
    )

    return {
        "run_id": public_run_id,
        "run_status": run.status,
        "active_card_id": run.active_card_id,
        "run_version": run.run_version,
        "events": [_event_to_dict(e) for e in events],
        "total": len(events),
        "has_more": len(events) >= limit,
    }


# ── Internal ─────────────────────────────────────────────────────────────────

def _event_to_dict(event: AgentRunEvent) -> dict[str, Any]:
    """将 AgentRunEvent 行转为前端可消费的 dict。"""
    return {
        "id": event.id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "patch_type": event.patch_type,
        "payload": event.payload_json,
        "idempotency_key": event.idempotency_key,
        "run_version": event.run_version,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
