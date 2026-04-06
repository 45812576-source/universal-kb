"""统一事件总线模型 — 支持实时通知与跨模块事件同步。"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class UnifiedEvent(Base):
    __tablename__ = "unified_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(100), nullable=False, index=True)
    source_type = Column(String(50), nullable=False)  # approval / skill / task / conversation
    source_id = Column(Integer, nullable=True)
    payload = Column(JSON, default=dict)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    workspace_id = Column(Integer, nullable=True)
    project_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
