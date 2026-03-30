"""Skill Memo 领域模型 — Skill Studio 状态机持久化。

一个 skill_id 只有一份当前有效 memo，采用结构化 JSON (memo_payload) 持久化，
避免首版过度拆表。
"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class SkillMemo(Base):
    __tablename__ = "skill_memos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), unique=True, nullable=False)

    # import_remediation / new_skill_creation / published_iteration
    scenario_type = Column(String(40), nullable=False)

    # analysis / planning / editing / awaiting_test / testing / fixing / ready_to_submit / completed
    lifecycle_stage = Column(String(40), nullable=False, default="analysis")

    status_summary = Column(Text, nullable=False, default="")
    goal_summary = Column(Text, nullable=True)

    # 首版唯一真相 JSON — 包含 package_analysis, persistent_notices, tasks,
    # current_task_id, progress_log, test_history, adopted_feedback, context_rollups
    memo_payload = Column(JSON, nullable=False, default=dict)

    version = Column(Integer, nullable=False, default=1)
    last_context_rollup = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    skill = relationship("Skill", foreign_keys=[skill_id])
