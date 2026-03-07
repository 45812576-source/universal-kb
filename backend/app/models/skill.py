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

    versions = relationship(
        "SkillVersion",
        back_populates="skill",
        order_by="SkillVersion.version.desc()",
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    system_prompt = Column(Text, nullable=False)
    variables = Column(JSON, default=list)  # ["{industry}", "{platform}"]
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=True)
    change_note = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", back_populates="versions")
    model_config = relationship("ModelConfig")
