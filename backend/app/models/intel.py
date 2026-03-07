import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class IntelSourceType(str, enum.Enum):
    RSS = "rss"
    CRAWLER = "crawler"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class IntelEntryStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class IntelSource(Base):
    __tablename__ = "intel_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    source_type = Column(Enum(IntelSourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    config = Column(JSON, default=dict)      # RSS: {url} / Crawler: {url, selector} / Webhook: {secret}
    schedule = Column(String(50), nullable=True)  # cron expression e.g. "0 8 * * *"
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class IntelEntry(Base):
    __tablename__ = "intel_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("intel_sources.id"), nullable=True)
    title = Column(String(500), nullable=False)
    content = Column(Text)
    url = Column(String(1000), nullable=True)
    tags = Column(JSON, default=list)
    industry = Column(String(100), nullable=True)
    platform = Column(String(100), nullable=True)
    status = Column(Enum(IntelEntryStatus, values_callable=lambda x: [e.value for e in x]), default=IntelEntryStatus.PENDING)
    auto_collected = Column(Boolean, default=True)
    vectorized = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
