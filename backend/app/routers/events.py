"""统一事件流 — SSE 端点，支持增量推送。"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.event_bus import UnifiedEvent
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

_SSE_POLL_INTERVAL = 2.0  # 秒
_SSE_TIMEOUT = 300  # 5 分钟


@router.get("/stream")
def event_stream(
    project_id: Optional[int] = Query(None),
    since: Optional[int] = Query(None, description="上次收到的最大 event id"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE 长连接，轮询 DB 返回增量事件。"""

    async def generate():
        last_id = since or 0
        elapsed = 0.0
        while elapsed < _SSE_TIMEOUT:
            q = db.query(UnifiedEvent).filter(UnifiedEvent.id > last_id)
            if project_id:
                q = q.filter(UnifiedEvent.project_id == project_id)
            events = q.order_by(UnifiedEvent.id.asc()).limit(50).all()

            for ev in events:
                data = {
                    "id": ev.id,
                    "event_type": ev.event_type,
                    "source_type": ev.source_type,
                    "source_id": ev.source_id,
                    "payload": ev.payload or {},
                    "user_id": ev.user_id,
                    "project_id": ev.project_id,
                    "created_at": ev.created_at.isoformat() if ev.created_at else None,
                }
                yield f"event: {ev.event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_id = ev.id

            if not events:
                yield ": ping\n\n"

            await asyncio.sleep(_SSE_POLL_INTERVAL)
            elapsed += _SSE_POLL_INTERVAL

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
