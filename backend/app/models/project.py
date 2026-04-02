"""项目模块数据模型：Project, ProjectMember, ProjectKnowledgeShare, ProjectReport, ProjectContext"""
import datetime
import enum

from sqlalchemy import Column, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class ProjectType(str, enum.Enum):
    DEV = "dev"
    CUSTOM = "custom"


class ProjectStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ReportType(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(ProjectStatus, values_callable=lambda x: [e.value for e in x]), default=ProjectStatus.DRAFT, nullable=False)
    project_type = Column(String(20), default="custom", nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    max_members = Column(Integer, default=5)
    llm_generated_plan = Column(JSON, nullable=True)
    governance_objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    resource_library_ids = Column(JSON, default=list)
    governance_kr_id = Column(Integer, ForeignKey("governance_krs.id"), nullable=True)
    governance_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    owner = relationship("User", foreign_keys=[owner_id])
    department = relationship("Department", foreign_keys=[department_id])
    members = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    reports = relationship("ProjectReport", back_populates="project", cascade="all, delete-orphan")
    contexts = relationship("ProjectContext", back_populates="project", cascade="all, delete-orphan")
    knowledge_shares = relationship("ProjectKnowledgeShare", back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role_desc = Column(Text, nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    task_order = Column(Integer, default=0)
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)

    project = relationship("Project", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])
    workspace = relationship("Workspace", foreign_keys=[workspace_id])


class ProjectKnowledgeShare(Base):
    __tablename__ = "project_knowledge_shares"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False)
    shared_at = Column(DateTime, default=datetime.datetime.utcnow)

    project = relationship("Project", back_populates="knowledge_shares")
    user = relationship("User", foreign_keys=[user_id])
    knowledge = relationship("KnowledgeEntry", foreign_keys=[knowledge_id])


class ProjectReport(Base):
    __tablename__ = "project_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    report_type = Column(Enum(ReportType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    content = Column(Text, nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    project = relationship("Project", back_populates="reports")


class ProjectContext(Base):
    __tablename__ = "project_contexts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    summary = Column(Text, nullable=True)
    requirements = Column(Text, nullable=True)
    acceptance_criteria = Column(Text, nullable=True)
    handoff_status = Column(String(20), default="none")
    handoff_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    project = relationship("Project", back_populates="contexts")
    workspace = relationship("Workspace", foreign_keys=[workspace_id])
