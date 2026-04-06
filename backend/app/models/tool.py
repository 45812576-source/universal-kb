import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class ToolType(str, enum.Enum):
    MCP = "mcp"
    BUILTIN = "builtin"
    HTTP = "http"


class ToolRegistry(Base):
    __tablename__ = "tool_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    tool_type = Column(Enum(ToolType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    config = Column(JSON, default=dict)
    input_schema = Column(JSON, default=dict)
    output_format = Column(String(50), default="json")
    is_active = Column(Boolean, default=True)
    scope = Column(String(20), default="personal")       # personal / department / company
    status = Column(String(20), default="draft")         # draft / published / archived
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    current_version = Column(Integer, default=1)

    skills = relationship("Skill", secondary="skill_tools", back_populates="bound_tools")
    versions = relationship("ToolVersion", backref="tool", order_by="ToolVersion.version.desc()")


class ToolVersionStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class ToolVersion(Base):
    """工具版本快照 — 每次 config/input_schema 变更时自动创建。"""
    __tablename__ = "tool_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    config_snapshot = Column(JSON, default=dict)
    input_schema_snapshot = Column(JSON, default=dict)
    status = Column(
        Enum(ToolVersionStatus, values_callable=lambda x: [e.value for e in x]),
        default=ToolVersionStatus.ACTIVE,
        nullable=False,
    )
    version_note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class UserSavedTool(Base):
    __tablename__ = "user_saved_tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=False)
    saved_at = Column(DateTime, default=datetime.datetime.utcnow)


class SkillTool(Base):
    __tablename__ = "skill_tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=False)
    pinned_version = Column(Integer, nullable=True)  # null=latest
