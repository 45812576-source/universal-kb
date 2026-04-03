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

    # ── 5维标签置信度 ────────────────────────────────────────────────────────
    content_tag_confidences = Column(JSON, nullable=True)
    # {subject_tag: 0.9, object_tag: 0.85, ...}

    # ── 摘要 ────────────────────────────────────────────────────────────────
    summary_short = Column(String(60), nullable=True)    # ≤50字 + 余量
    summary_search = Column(String(500), nullable=True)
    summary_embedding = Column(String(500), nullable=True)  # 向量检索专用摘要（不脱敏）
    summary_sensitivity_mode = Column(String(20), nullable=True)  # raw/masked/abstracted

    # ── 系统编号 ────────────────────────────────────────────────────────────
    system_id = Column(String(30), unique=True, index=True, nullable=True)

    # ── 来源追踪 ────────────────────────────────────────────────────────────
    classification_source = Column(String(20), nullable=True)   # rule/llm/mixed/fallback
    tagging_source = Column(String(20), nullable=True)
    masking_source = Column(String(20), nullable=True)
    summarization_source = Column(String(20), nullable=True)

    # ── 用户确认 ──────────────────────────────────────────────────────────
    confirmed_at = Column(DateTime, nullable=True)       # 用户确认时间，None=未确认
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # 用户确认时的修正内容（仅存用户改过的字段）
    user_corrections = Column(JSON, nullable=True)

    # ── 脱敏规则版本 & 纠错状态 ──────────────────────────────────────────────
    mask_rule_version = Column(Integer, nullable=True)
    correction_status = Column(String(20), nullable=True)  # pending_correction / corrected / null

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
