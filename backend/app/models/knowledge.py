import datetime
import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class KnowledgeStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(50), default="experience")  # experience / external_intel
    status = Column(Enum(KnowledgeStatus), default=KnowledgeStatus.PENDING)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    review_note = Column(Text, nullable=True)

    # Metadata for hard filtering in Milvus
    industry_tags = Column(JSON, default=list)   # ["食品", "美妆"]
    platform_tags = Column(JSON, default=list)   # ["抖音", "小红书"]
    topic_tags = Column(JSON, default=list)      # ["投放策略", "客户拓展"]

    # Source info
    source_type = Column(String(50), default="manual")  # manual / upload / auto_collected
    source_file = Column(String(255), nullable=True)

    # Milvus chunk IDs
    milvus_ids = Column(JSON, default=list)

    source_draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    raw_input_id    = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    capture_mode    = Column(String(50), default="manual_form")
    visibility_scope = Column(String(50), nullable=True)
    linked_skill_codes = Column(JSON, default=list)
    applicable_departments = Column(JSON, default=list)
    applicable_roles = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    creator = relationship("User", foreign_keys=[created_by])
    reviewer = relationship("User", foreign_keys=[reviewed_by])
