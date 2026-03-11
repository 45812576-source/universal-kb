import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class IntelSourceType(str, enum.Enum):
    RSS = "rss"
    CRAWLER = "crawler"
    DEEP_CRAWL = "deep_crawl"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class IntelEntryStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class IntelTaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IntelSource(Base):
    __tablename__ = "intel_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    source_type = Column(Enum(IntelSourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    config = Column(JSON, default=dict)
    # RSS: {url}
    # Crawler: {url, js_render, wait_selector, extract_strategy, ...}
    # Deep Crawl: {url, max_depth, max_pages, filters, include_external, ...}
    schedule = Column(String(50), nullable=True)  # cron expression e.g. "0 8 * * *"
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Permission fields: who manages this source (assigned by admin offline)
    managed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    authorized_user_ids = Column(JSON, default=list)  # list of user IDs with read/operate access


class IntelEntry(Base):
    __tablename__ = "intel_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("intel_sources.id"), nullable=True)
    title = Column(String(500), nullable=False)
    content = Column(Text)
    raw_markdown = Column(Text, nullable=True)  # crawl4ai 提取的原始 Markdown
    url = Column(String(1000), nullable=True)
    tags = Column(JSON, default=list)
    industry = Column(String(100), nullable=True)
    platform = Column(String(100), nullable=True)
    depth = Column(Integer, default=0)  # 爬取深度层级
    status = Column(Enum(IntelEntryStatus, values_callable=lambda x: [e.value for e in x]), default=IntelEntryStatus.PENDING)
    auto_collected = Column(Boolean, default=True)
    vectorized = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)


class IntelTask(Base):
    __tablename__ = "intel_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("intel_sources.id"), nullable=True)
    status = Column(Enum(IntelTaskStatus, values_callable=lambda x: [e.value for e in x]), default=IntelTaskStatus.QUEUED)
    total_urls = Column(Integer, default=0)
    crawled_urls = Column(Integer, default=0)
    new_entries = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
