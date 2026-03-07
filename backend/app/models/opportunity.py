import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base


class OpportunityStage(str, enum.Enum):
    LEAD = "lead"
    CONTACT = "contact"
    NEEDS = "needs"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"


class OpportunityStatus(str, enum.Enum):
    ACTIVE = "active"
    WON = "won"
    LOST = "lost"
    ON_HOLD = "on_hold"


class Opportunity(Base):
    __tablename__ = "opportunities"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    title            = Column(String(200), nullable=False)
    customer_name    = Column(String(200), nullable=True)
    industry         = Column(String(100), nullable=True)
    stage            = Column(Enum(OpportunityStage), default=OpportunityStage.LEAD)
    priority         = Column(String(20), default="normal")
    needs_summary    = Column(Text, nullable=True)
    decision_map     = Column(JSON, default=list)
    risk_points      = Column(JSON, default=list)
    next_actions     = Column(JSON, default=list)
    source_draft_id  = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    created_by_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id    = Column(Integer, ForeignKey("departments.id"), nullable=True)
    status           = Column(Enum(OpportunityStatus), default=OpportunityStatus.ACTIVE)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
