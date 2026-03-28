import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class OpenCodeWorkspaceMapping(Base):
    __tablename__ = "opencode_workspace_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    opencode_workspace_id = Column(String(255), nullable=True)  # 兼容旧数据，新数据用 directory
    opencode_workspace_name = Column(String(255), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    directory = Column(String(1024), nullable=True)  # 用户 opencode workdir，按此匹配 session
    oss_prefix = Column(String(500), nullable=True)  # OSS 路径前缀，如 studio_workspaces/胡瑞
    kb_folder_id = Column(Integer, nullable=True)    # 对应知识库"开发工地"文件夹 ID
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class OpenCodeUsageCache(Base):
    """每 12 小时快照一次的 OpenCode 用量聚合缓存，按用户存储。"""
    __tablename__ = "opencode_usage_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    sessions = Column(Integer, default=0)
    ai_calls = Column(Integer, default=0)          # step-finish 条数，即 LLM API 调用次数
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    files_changed = Column(Integer, default=0)
    lines_added = Column(Integer, default=0)
    lines_deleted = Column(Integer, default=0)
    models = Column(JSON, default=dict)    # {model_id: call_count}
    workspaces = Column(JSON, default=list)  # [workspace_name, ...]
    output_files = Column(JSON, default=list)  # [{path, session_title}, ...]
    skills_submitted = Column(Integer, default=0)
    tools_submitted = Column(Integer, default=0)
    computed_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])


class UserModelGrant(Base):
    """记录哪些用户被授权使用受限模型（如 lemondata/gpt-5.4）。"""
    __tablename__ = "user_model_grants"
    __table_args__ = (UniqueConstraint("user_id", "model_key", name="uq_user_model_grant"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    model_key = Column(String(100), nullable=False)  # e.g. "lemondata/gpt-5.4"
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
