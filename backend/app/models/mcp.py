import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class McpSource(Base):
    __tablename__ = "mcp_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    adapter_type = Column(String(20), default="mcp")    # mcp / rest
    auth_token = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class McpTokenScope(str, enum.Enum):
    USER = "user"
    WORKSPACE = "workspace"
    ADMIN = "admin"


class McpToken(Base):
    __tablename__ = "mcp_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    token_hash = Column(String(200), nullable=False, unique=True)
    token_prefix = Column(String(12), nullable=False)
    scope = Column(Enum(McpTokenScope), default=McpTokenScope.USER)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class SkillUpstreamCheck(Base):
    __tablename__ = "skill_upstream_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    checked_at = Column(DateTime, default=datetime.datetime.utcnow)
    upstream_version = Column(String(50), nullable=True)
    has_diff = Column(Boolean, default=False)
    diff_summary = Column(Text, nullable=True)
    action = Column(String(20), default="pending")       # pending / synced / ignored
