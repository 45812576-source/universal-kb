import datetime
import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class SkillMode(str, enum.Enum):
    STRUCTURED = "structured"
    UNSTRUCTURED = "unstructured"
    HYBRID = "hybrid"


class SkillStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)  # openai / deepseek / anthropic
    model_id = Column(String(100), nullable=False)  # deepseek-chat / gpt-4o
    api_base = Column(String(255))
    api_key_env = Column(String(100))  # env var name, not the key itself
    max_tokens = Column(Integer, default=4096)
    temperature = Column(String(10), default="0.7")
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ModelAssignment(Base):
    """调用点 → 模型配置 绑定表。slot_key 对应 SLOT_REGISTRY 中的 key。"""
    __tablename__ = "model_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_key = Column(String(100), unique=True, nullable=False)
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    model_config = relationship("ModelConfig")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    mode = Column(Enum(SkillMode), default=SkillMode.HYBRID)
    status = Column(Enum(SkillStatus), default=SkillStatus.DRAFT)
    knowledge_tags = Column(JSON, default=list)
    auto_inject = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    data_queries = Column(JSON, default=list)  # quick access copy of SkillDataQuery entries
    tools = Column(JSON, default=list)  # tool declarations

    bound_tools = relationship(
        "ToolRegistry",
        secondary="skill_tools",
        back_populates="skills",
    )
    versions = relationship(
        "SkillVersion",
        back_populates="skill",
        order_by="SkillVersion.version.desc()",
        cascade="all, delete-orphan",
    )
    suggestions = relationship(
        "SkillSuggestion",
        foreign_keys="SkillSuggestion.skill_id",
        cascade="all, delete-orphan",
        overlaps="skill",
    )
    attributions = relationship(
        "SkillAttribution",
        foreign_keys="SkillAttribution.skill_id",
        cascade="all, delete-orphan",
        overlaps="skill",
    )

    # Scope: personal / department / company
    scope = Column(String(20), default="personal")

    # 是否在 Skill 执行后自动触发"沉淀为知识"
    auto_save_output = Column(Boolean, default=False)

    # 复杂 Skill 的附属文件（zip 包上传时提取的非 .md 文件）
    # 每项格式: {"filename": "ref.py", "path": "uploads/skills/42/ref.py", "size": 1234, "category": "tool"}
    # category: knowledge-base | reference | example | tool | template | other
    source_files = Column(JSON, default=list, nullable=True)

    # Upstream tracking fields
    source_type = Column(String(20), default="local")  # local / imported / forked
    upstream_url = Column(String(500), nullable=True)
    upstream_id = Column(String(200), nullable=True)
    upstream_version = Column(String(50), nullable=True)
    upstream_content = Column(Text, nullable=True)  # 永远保存上游原版 system_prompt
    upstream_synced_at = Column(DateTime, nullable=True)
    is_customized = Column(Boolean, default=False)
    parent_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    local_modified_at = Column(DateTime, nullable=True)


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    system_prompt = Column(Text, nullable=False)
    variables = Column(JSON, default=list)  # ["{industry}", "{platform}"]
    required_inputs = Column(JSON, default=list)  # [{"key": "product", "label": "产品名称", "desc": "你的具体产品是什么", "example": "XX猫粮"}]
    output_schema = Column(JSON, default=None)  # JSON Schema defining structured output
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=True)
    change_note = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", back_populates="versions")
    model_config = relationship("ModelConfig")


class SuggestionStatus(str, enum.Enum):
    PENDING = "pending"
    ADOPTED = "adopted"
    PARTIAL = "partial"
    REJECTED = "rejected"


class SkillSuggestion(Base):
    __tablename__ = "skill_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    problem_desc = Column(Text, nullable=False)
    expected_direction = Column(Text, nullable=False)
    case_example = Column(Text, nullable=True)
    status = Column(
        Enum(SuggestionStatus, values_callable=lambda x: [e.value for e in x]),
        default=SuggestionStatus.PENDING,
    )
    review_note = Column(Text, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Comment/reaction source fields
    source_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    reaction_type = Column(String(20), nullable=True)  # "like" / "comment"

    skill = relationship("Skill", foreign_keys=[skill_id], overlaps="suggestions")
    submitter = relationship("User", foreign_keys=[submitted_by])
    reviewer = relationship("User", foreign_keys=[reviewed_by])


class AttributionLevel(str, enum.Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class UserSavedSkill(Base):
    """用户保存的公司级 Skill（从市场收藏）。"""
    __tablename__ = "user_saved_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    saved_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])


class SkillPreflightResult(Base):
    """Skill 预检结果持久化 — 支持增量检测（content_hash 比对跳过未变维度）。"""
    __tablename__ = "skill_preflight_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    gate_name = Column(String(50), nullable=False)  # structure / knowledge / tools / quality
    passed = Column(Boolean, nullable=False)
    score = Column(Integer, nullable=True)  # NULL for gates, 0-100 for quality
    detail = Column(JSON, nullable=True)
    content_hash = Column(String(64), nullable=True)  # 内容变更检测
    checked_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])


class SkillAttribution(Base):
    __tablename__ = "skill_attributions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    version_from = Column(Integer, nullable=False)
    version_to = Column(Integer, nullable=False)
    suggestion_id = Column(Integer, ForeignKey("skill_suggestions.id"), nullable=False)
    attribution_level = Column(Enum(AttributionLevel), nullable=False)
    matched_change = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id], overlaps="attributions")
    suggestion = relationship("SkillSuggestion", foreign_keys=[suggestion_id])
