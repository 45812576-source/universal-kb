"""知识资产细粒度权限 & 权限变更请求

- KnowledgePermissionGrant: 细粒度动作级授权（替代旧的 KnowledgeFolderGrant 二布尔值模型）
- PermissionChangeRequest: 权限变更工单（高风险操作走审批流）
"""
import datetime
import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


# ─── 枚举 ────────────────────────────────────────────────────────────────────

class PermissionResourceType(str, enum.Enum):
    FOLDER = "folder"
    APPROVAL_CAPABILITY = "approval_capability"


class PermissionScope(str, enum.Enum):
    EXACT = "exact"
    SUBTREE = "subtree"


class PermissionSource(str, enum.Enum):
    DIRECT = "direct"
    APPROVAL = "approval"
    ROLE_DEFAULT = "role_default"


class PermissionChangeDomain(str, enum.Enum):
    FEATURE_FLAG = "feature_flag"
    MODEL_GRANT = "model_grant"
    CAPABILITY_GRANT = "capability_grant"


class PermissionChangeStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ─── 模型 ────────────────────────────────────────────────────────────────────

class KnowledgePermissionGrant(Base):
    """细粒度知识资产权限授权。

    resource_type = folder → resource_id 指向 knowledge_folders.id
    resource_type = approval_capability → resource_id 为 NULL（全局能力）

    action 示例:
      knowledge.folder.view / knowledge.folder.create_child / ...
      knowledge.review.approve / skill.publish.approve_final / ...
    """
    __tablename__ = "knowledge_permission_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grantee_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    resource_type = Column(
        Enum(PermissionResourceType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    resource_id = Column(Integer, nullable=True)  # folder_id or NULL
    action = Column(String(100), nullable=False)
    scope = Column(
        Enum(PermissionScope, values_callable=lambda x: [e.value for e in x]),
        default=PermissionScope.EXACT,
    )
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    source = Column(
        Enum(PermissionSource, values_callable=lambda x: [e.value for e in x]),
        default=PermissionSource.DIRECT,
    )

    grantee = relationship("User", foreign_keys=[grantee_user_id])
    grantor = relationship("User", foreign_keys=[granted_by])


class PermissionChangeRequest(Base):
    """权限变更请求。高风险操作先创建工单，审批通过后生效。"""
    __tablename__ = "permission_change_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    domain = Column(
        Enum(PermissionChangeDomain, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    action_key = Column(String(100), nullable=False)   # e.g. "dev_studio", "skill.publish.approve_final"
    current_value = Column(JSON, nullable=True)
    target_value = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    risk_note = Column(Text, nullable=True)

    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(
        Enum(PermissionChangeStatus, values_callable=lambda x: [e.value for e in x]),
        default=PermissionChangeStatus.PENDING,
    )
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    review_comment = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    target_user = relationship("User", foreign_keys=[target_user_id])
    requester = relationship("User", foreign_keys=[requester_id])
    reviewer = relationship("User", foreign_keys=[reviewer_id])
