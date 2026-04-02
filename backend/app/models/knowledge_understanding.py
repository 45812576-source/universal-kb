"""文档理解 Profile：统一存储文档理解流水线的全部产出。

与 knowledge_entries 一对一关联，通过 knowledge_id 外键。
"""
import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class KnowledgeUnderstandingProfile(Base):
    __tablename__ = "knowledge_understanding_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(
        Integer,
        ForeignKey("knowledge_entries.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # ── 自动命名 ────────────────────────────────────────────────────────────
    display_title = Column(String(500), nullable=True)
    raw_title = Column(String(500), nullable=True)
    title_confidence = Column(Float, nullable=True)
    title_source = Column(String(30), nullable=True)   # user/ai/cleaned_filename/fallback
    title_reason = Column(Text, nullable=True)

    # ── 分类与权限标签 ──────────────────────────────────────────────────────
    document_type = Column(String(50), nullable=True)       # 受控枚举
    permission_domain = Column(String(50), nullable=True)
    desensitization_level = Column(String(20), nullable=True)  # D0~D4
    contains_sensitive_data = Column(Boolean, default=False)
    data_type_hits = Column(JSON, default=list)              # [{type, field, count, sample}]
    visibility_recommendation = Column(String(30), nullable=True)

    # ── 5维内容标签 ─────────────────────────────────────────────────────────
    content_tags = Column(JSON, nullable=True)
    # {subject_tag, object_tag, scenario_tag, action_tag, industry_or_domain_tag}
    suggested_tags = Column(JSON, default=list)

    # ── 摘要 ────────────────────────────────────────────────────────────────
    summary_short = Column(String(200), nullable=True)
    summary_search = Column(String(500), nullable=True)
    summary_sensitivity_mode = Column(String(20), nullable=True)  # raw/masked/abstracted

    # ── 来源追踪 ────────────────────────────────────────────────────────────
    classification_source = Column(String(20), nullable=True)   # rule/llm/mixed/fallback
    tagging_source = Column(String(20), nullable=True)
    masking_source = Column(String(20), nullable=True)
    summarization_source = Column(String(20), nullable=True)

    # ── 流水线状态 ──────────────────────────────────────────────────────────
    understanding_status = Column(String(20), default="pending", nullable=False)
    # pending/running/success/partial/failed
    understanding_error = Column(Text, nullable=True)
    understanding_version = Column(Integer, default=1)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    knowledge = relationship("KnowledgeEntry", foreign_keys=[knowledge_id])
