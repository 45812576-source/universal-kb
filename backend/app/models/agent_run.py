"""Agent Run / Event 持久化模型 — DB-backed run lifecycle + append-only event log.

Phase B1/B2: 统一 public_run_id，支持 replay、恢复、审计。
"""
import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class AgentRun(Base):
    """一次 Studio Run 的持久化记录。

    public_run_id 是前端唯一 run 身份；harness_run_id 仅做内层审计。
    """
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_run_id = Column(String(64), nullable=False, unique=True, index=True)
    harness_run_id = Column(String(64), nullable=True, index=True)
    parent_run_id = Column(String(64), nullable=True, index=True)
    conversation_id = Column(Integer, nullable=False, index=True)
    skill_id = Column(Integer, nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    active_card_id = Column(String(64), nullable=True)
    run_version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="queued", index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    superseded_by = Column(String(64), nullable=True)
    message_id = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_agent_runs_conv_status", "conversation_id", "status"),
    )


class AgentRunEvent(Base):
    """Run 内的 append-only event log，支持 after_sequence replay。

    sequence 在同一 public_run_id 内单调递增。
    """
    __tablename__ = "agent_run_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_run_id = Column(String(64), nullable=False, index=True)
    run_version = Column(Integer, nullable=False, default=1)
    harness_run_id = Column(String(64), nullable=True)
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(100), nullable=False, index=True)
    patch_type = Column(String(64), nullable=True)
    payload_json = Column(JSON, default=dict)
    idempotency_key = Column(String(128), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_agent_run_events_run_seq", "public_run_id", "sequence"),
    )
