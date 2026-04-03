from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base
from app.utils.time_utils import utcnow


class KnowledgeShareLink(Base):
    __tablename__ = "knowledge_share_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    share_token = Column(String(120), nullable=False, unique=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    access_scope = Column(String(50), default="public_readonly", nullable=False)
    expires_at = Column(DateTime, nullable=True)
    last_accessed_at = Column(DateTime, nullable=True)
    access_count = Column(Integer, default=0, nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    knowledge = relationship("KnowledgeEntry")
    creator = relationship("User")
