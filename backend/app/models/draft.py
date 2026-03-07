import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base
from app.models.raw_input import DetectedObjectType


class DraftStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"
    CONVERTED = "converted"


class Draft(Base):
    __tablename__ = "drafts"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    object_type          = Column(Enum(DetectedObjectType), nullable=False)
    source_raw_input_id  = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    source_extraction_id = Column(Integer, ForeignKey("input_extractions.id"), nullable=True)
    conversation_id      = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    title                = Column(String(200), nullable=True)
    summary              = Column(Text, nullable=True)
    fields_json          = Column(JSON, default=dict)
    tags_json            = Column(JSON, default=dict)
    pending_questions    = Column(JSON, default=list)
    confirmed_fields     = Column(JSON, default=dict)
    user_corrections     = Column(JSON, default=list)
    suggested_actions    = Column(JSON, default=list)
    status               = Column(Enum(DraftStatus), default=DraftStatus.WAITING_CONFIRMATION)
    formal_object_id     = Column(Integer, nullable=True)
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at           = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class LearningSample(Base):
    __tablename__ = "learning_samples"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id         = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    draft_id             = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    object_type          = Column(String(50), nullable=False)
    task_type            = Column(String(50), nullable=True)
    model_output_json    = Column(JSON, default=dict)
    user_correction_json = Column(JSON, default=dict)
    final_answer_json    = Column(JSON, default=dict)
    created_by_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
