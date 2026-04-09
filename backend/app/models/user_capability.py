"""用户资格能力授权模型

全局能力资格（如知识资产管理员、Skill 发布审批员），
决定用户可管理哪类资产 / 审哪类工单。
"""
import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class UserCapabilityGrant(Base):
    __tablename__ = "user_capability_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    capability_key = Column(String(100), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    source = Column(String(20), default="direct")  # direct | approval | role_default
    scope_json = Column(JSON, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    grantor = relationship("User", foreign_keys=[granted_by])
