"""知识库管理后台模型 V1.5

- KnowledgeFolderGrant: 子树委派授权
- KnowledgeFolderAuditLog: 目录变更审计
- KnowledgeRerunJob: 重绑作业
"""
import datetime
import enum

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Integer, String, Text,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class FolderGrantScope(str, enum.Enum):
    SUBTREE = "subtree"


class FolderAuditAction(str, enum.Enum):
    RENAME = "rename"
    MOVE = "move"
    DELETE = "delete"
    CREATE = "create"
    SORT = "sort"
    GRANT = "grant"
    REVOKE = "revoke"
    RERUN_TRIGGER = "rerun_trigger"


class RerunTriggerType(str, enum.Enum):
    FOLDER_RENAME = "folder_rename"
    FOLDER_MOVE = "folder_move"
    FOLDER_DELETE = "folder_delete"
    MANUAL = "manual"


class RerunTargetScope(str, enum.Enum):
    SUBTREE = "subtree"
    SINGLE = "single"


class RerunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class KnowledgeFolderGrant(Base):
    """子树委派授权：超管把系统目录子树的管理权委派给指定用户。"""
    __tablename__ = "knowledge_folder_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=False)
    grantee_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    scope = Column(
        Enum(FolderGrantScope, values_callable=lambda x: [e.value for e in x]),
        default=FolderGrantScope.SUBTREE,
    )
    can_manage_children = Column(Boolean, default=True)
    can_delete_descendants = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    folder = relationship("KnowledgeFolder", foreign_keys=[folder_id])
    grantee = relationship("User", foreign_keys=[grantee_user_id])
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        UniqueConstraint("folder_id", "grantee_user_id", name="uq_folder_grant"),
    )


class KnowledgeFolderAuditLog(Base):
    """目录变更审计日志。"""
    __tablename__ = "knowledge_folder_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=False)
    action = Column(
        Enum(FolderAuditAction, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    performed_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    folder = relationship("KnowledgeFolder", foreign_keys=[folder_id])
    performer = relationship("User", foreign_keys=[performed_by])


class KnowledgeRerunJob(Base):
    """重绑作业：目录变更后对文档 folder 重绑 + 编号重算。"""
    __tablename__ = "knowledge_rerun_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_type = Column(
        Enum(RerunTriggerType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=False)
    target_scope = Column(
        Enum(RerunTargetScope, values_callable=lambda x: [e.value for e in x]),
        default=RerunTargetScope.SUBTREE,
    )
    status = Column(
        Enum(RerunStatus, values_callable=lambda x: [e.value for e in x]),
        default=RerunStatus.PENDING,
    )
    affected_count = Column(Integer, default=0)
    reclassified_count = Column(Integer, default=0)
    renamed_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    error_log = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    target_folder = relationship("KnowledgeFolder", foreign_keys=[target_folder_id])
    creator = relationship("User", foreign_keys=[created_by])
