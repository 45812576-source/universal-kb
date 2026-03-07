import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base


class RawInputSourceType(str, enum.Enum):
    TEXT = "text"
    VOICE = "voice"
    FILE = "file"
    IMAGE = "image"
    URL = "url"
    PASTE = "paste"
    MULTI = "multi"


class RawInputStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"


class DetectedObjectType(str, enum.Enum):
    KNOWLEDGE = "knowledge"
    OPPORTUNITY = "opportunity"
    FEEDBACK = "feedback"
    UNKNOWN = "unknown"


class RawInput(Base):
    __tablename__ = "raw_inputs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id    = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_type     = Column(Enum(RawInputSourceType), nullable=False, default=RawInputSourceType.TEXT)
    source_channel  = Column(String(50), default="web")
    raw_text        = Column(Text, nullable=True)
    attachment_urls = Column(JSON, default=list)
    context_json    = Column(JSON, default=dict)
    status          = Column(Enum(RawInputStatus), default=RawInputStatus.RECEIVED)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class InputExtraction(Base):
    __tablename__ = "input_extractions"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id         = Column(Integer, ForeignKey("raw_inputs.id"), nullable=False, unique=True)
    detected_intent      = Column(String(200), nullable=True)
    detected_object_type = Column(Enum(DetectedObjectType), nullable=False, default=DetectedObjectType.UNKNOWN)
    summary              = Column(Text, nullable=True)
    entities_json        = Column(JSON, default=dict)
    fields_json          = Column(JSON, default=dict)
    confidence_json      = Column(JSON, default=dict)
    uncertain_fields     = Column(JSON, default=list)
    extractor_version    = Column(String(50), default="v1")
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
