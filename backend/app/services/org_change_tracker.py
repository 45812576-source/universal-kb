"""组织管理变更追踪 — 拦截所有 org 数据写入，自动记录 org_change_events"""

import datetime
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.org_management import OrgChangeEvent

logger = logging.getLogger(__name__)


def _serialize_value(val: Any) -> Any:
    """将 SQLAlchemy 模型字段值转为可 JSON 序列化的值"""
    if val is None:
        return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()
    if isinstance(val, (int, float, str, bool)):
        return val
    return str(val)


def compute_field_changes(old_dict: dict, new_dict: dict) -> list[dict]:
    """对比两个字典，返回 [{field, old_value, new_value}] 的变更列表"""
    changes = []
    all_keys = set(old_dict.keys()) | set(new_dict.keys())
    skip_keys = {"id", "created_at", "updated_at"}
    for key in sorted(all_keys):
        if key in skip_keys:
            continue
        old_val = _serialize_value(old_dict.get(key))
        new_val = _serialize_value(new_dict.get(key))
        if old_val != new_val:
            changes.append({"field": key, "old_value": old_val, "new_value": new_val})
    return changes


def model_to_dict(instance) -> dict:
    """将 SQLAlchemy 模型实例转为 dict（只取 Column 字段）"""
    return {c.key: getattr(instance, c.key, None) for c in instance.__table__.columns}


def track_change(
    db: Session,
    entity_type: str,
    entity_id: int,
    change_type: str,
    user_id: int | None = None,
    source: str = "manual",
    field_changes: list[dict] | None = None,
    import_session_id: int | None = None,
    baseline_version: str | None = None,
):
    """记录一条组织变更事件"""
    event = OrgChangeEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        change_type=change_type,
        field_changes=field_changes or [],
        change_source=source,
        import_session_id=import_session_id,
        baseline_version=baseline_version,
        created_by=user_id,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(event)
    return event


def track_create(db: Session, entity_type: str, instance, user_id: int | None = None,
                 source: str = "manual", import_session_id: int | None = None):
    """记录创建事件"""
    new_dict = model_to_dict(instance)
    changes = [{"field": k, "old_value": None, "new_value": _serialize_value(v)}
               for k, v in new_dict.items() if k not in ("id", "created_at", "updated_at") and v is not None]
    return track_change(db, entity_type, instance.id, "created", user_id, source, changes, import_session_id)


def track_update(db: Session, entity_type: str, entity_id: int, old_dict: dict, new_dict: dict,
                 user_id: int | None = None, source: str = "manual", import_session_id: int | None = None):
    """记录更新事件（需提前保存旧值）"""
    changes = compute_field_changes(old_dict, new_dict)
    if not changes:
        return None
    return track_change(db, entity_type, entity_id, "updated", user_id, source, changes, import_session_id)


def track_delete(db: Session, entity_type: str, instance, user_id: int | None = None, source: str = "manual"):
    """记录删除事件"""
    old_dict = model_to_dict(instance)
    changes = [{"field": k, "old_value": _serialize_value(v), "new_value": None}
               for k, v in old_dict.items() if k not in ("id", "created_at", "updated_at") and v is not None]
    return track_change(db, entity_type, instance.id, "deleted", user_id, source, changes)
