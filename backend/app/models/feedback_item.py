import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base


class FeedbackType(str, enum.Enum):
    BUG = "bug"
    FEATURE_REQUEST = "feature_request"
    CONFIG_ISSUE = "config_issue"
    TRAINING_ISSUE = "training_issue"
    CHURN_RISK = "churn_risk"


class FeedbackItem(Base):
    __tablename__ = "feedback_items"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    title              = Column(String(200), nullable=True)
    customer_name      = Column(String(200), nullable=True)
    feedback_type      = Column(Enum(FeedbackType), nullable=True)
    severity           = Column(String(20), default="medium")
    description        = Column(Text, nullable=True)
    affected_module    = Column(String(100), nullable=True)
    renewal_risk_level = Column(String(20), default="low")
    routed_team        = Column(String(100), nullable=True)
    knowledgeworthy    = Column(Integer, default=0)
    source_draft_id    = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    created_by_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    status             = Column(String(20), default="open")
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
