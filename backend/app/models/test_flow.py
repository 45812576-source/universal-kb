import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class TestFlowRunLink(Base):
    __tablename__ = "test_flow_run_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    report_id = Column(Integer, ForeignKey("sandbox_test_reports.id", ondelete="SET NULL"), nullable=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("test_case_plan_drafts.id", ondelete="SET NULL"), nullable=True)
    plan_version = Column(Integer, nullable=True)
    case_count = Column(Integer, nullable=False, default=0)
    entry_source = Column(String(32), nullable=True)
    decision_mode = Column(String(32), nullable=True)
    conversation_id = Column(Integer, nullable=True)
    workflow_id = Column(Integer, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    session = relationship("SandboxTestSession", foreign_keys=[session_id])
    skill = relationship("Skill", foreign_keys=[skill_id])
    plan = relationship("TestCasePlanDraft", foreign_keys=[plan_id])
