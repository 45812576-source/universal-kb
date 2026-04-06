"""统一事件总线 — 事件发射 + 查询。"""
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.event_bus import UnifiedEvent

logger = logging.getLogger(__name__)


def emit(
    db: Session,
    event_type: str,
    source_type: str,
    source_id: int | None = None,
    payload: dict[str, Any] | None = None,
    user_id: int | None = None,
    workspace_id: int | None = None,
    project_id: int | None = None,
) -> UnifiedEvent:
    """发射一个事件到统一事件总线。"""
    event = UnifiedEvent(
        event_type=event_type,
        source_type=source_type,
        source_id=source_id,
        payload=payload or {},
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
    )
    db.add(event)
    try:
        db.commit()
        db.refresh(event)
    except Exception:
        db.rollback()
        logger.warning("Failed to emit event", exc_info=True)
    return event
