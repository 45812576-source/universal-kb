"""Skill 知识引用审核快照 + 脱敏纠错反馈 + 脱敏规则版本。

- SkillKnowledgeReference: Skill 发布时审过的知识文件引用集合 + 脱敏快照
- KnowledgeMaskFeedback: 脱敏纠错建议
- KnowledgeMaskRuleVersion: 脱敏规则版本（监督式更新产出）
"""
import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class SkillKnowledgeReference(Base):
    """Skill 发布时审过的知识文件引用集合 + 脱敏快照"""
    __tablename__ = "skill_knowledge_references"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False)
    snapshot_desensitization_level = Column(String(20), nullable=True)
    snapshot_data_type_hits = Column(JSON, default=list)
    snapshot_document_type = Column(String(50), nullable=True)
    snapshot_permission_domain = Column(String(50), nullable=True)
    snapshot_mask_rules = Column(JSON, default=list)  # 生效的脱敏规则集
    mask_rule_source = Column(String(30), nullable=True)  # rule/llm/manual
    folder_id = Column(Integer, nullable=True)
    folder_path = Column(String(500), nullable=True)
    manager_scope_ok = Column(Boolean, default=False)
    publish_version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    knowledge = relationship("KnowledgeEntry", foreign_keys=[knowledge_id])

    __table_args__ = (
        UniqueConstraint("skill_id", "knowledge_id", "publish_version", name="uq_skill_knowledge_ref"),
    )


class KnowledgeMaskFeedback(Base):
    """脱敏纠错建议"""
    __tablename__ = "knowledge_mask_feedbacks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    understanding_profile_id = Column(Integer, ForeignKey("knowledge_understanding_profiles.id"), nullable=True)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    current_desensitization_level = Column(String(20), nullable=True)
    current_data_type_hits = Column(JSON, default=list)
    suggested_desensitization_level = Column(String(20), nullable=True)
    suggested_data_type_adjustments = Column(JSON, default=list)
    reason = Column(Text, nullable=False)
    evidence_snippet = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending/approved/rejected
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=True)
    review_action = Column(String(30), nullable=True)  # update_file/update_rule
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    knowledge = relationship("KnowledgeEntry", foreign_keys=[knowledge_id])
    submitter = relationship("User", foreign_keys=[submitted_by])
    reviewer = relationship("User", foreign_keys=[reviewed_by])


class KnowledgeMaskRuleVersion(Base):
    """脱敏规则版本（监督式更新产出）"""
    __tablename__ = "knowledge_mask_rule_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, unique=True, nullable=False)
    changes = Column(JSON, default=list)  # [{feedback_id, change_type, before, after}]
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    approver = relationship("User", foreign_keys=[approved_by])
