import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class WorkspaceStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(50), default="chat")
    color = Column(String(20), default="#00D1FF")
    category = Column(String(50), default="通用")
    status = Column(Enum(WorkspaceStatus), default=WorkspaceStatus.DRAFT, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    visibility = Column(String(20), default="all")  # all / department
    welcome_message = Column(Text, default="你好，有什么可以帮你的？")
    system_context = Column(Text, nullable=True)
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=True)
    workspace_type = Column(String(20), default="chat", nullable=False)  # chat | opencode
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    workspace_skills = relationship("WorkspaceSkill", back_populates="workspace", cascade="all, delete-orphan")
    workspace_tools = relationship("WorkspaceTool", back_populates="workspace", cascade="all, delete-orphan")
    workspace_data_tables = relationship("WorkspaceDataTable", back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceSkill(Base):
    __tablename__ = "workspace_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)

    workspace = relationship("Workspace", back_populates="workspace_skills")
    skill = relationship("Skill", foreign_keys=[skill_id])


class WorkspaceTool(Base):
    __tablename__ = "workspace_tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=False)

    workspace = relationship("Workspace", back_populates="workspace_tools")
    tool = relationship("ToolRegistry", foreign_keys=[tool_id])


class WorkspaceDataTable(Base):
    __tablename__ = "workspace_data_tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    table_name = Column(String(200), nullable=False)

    workspace = relationship("Workspace", back_populates="workspace_data_tables")
