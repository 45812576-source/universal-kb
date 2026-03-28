"""飞书审批实例记录 — 跟踪 AI 发起的审批流程状态。"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class LarkApprovalInstance(Base):
    __tablename__ = "lark_approval_instances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instance_code = Column(String(100), unique=True, nullable=False)   # 飞书返回的审批实例 code
    approval_code = Column(String(100), nullable=False)                # 审批定义 code（模板）
    title = Column(String(200))
    status = Column(String(20), default="PENDING")  # PENDING / APPROVED / REJECTED / CANCELED / DELETED
    form_data = Column(JSON, nullable=True)           # 提交的表单快照
    result_data = Column(JSON, nullable=True)         # 审批完成后结果详情（timeline, comments 等）
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    user = relationship("User", foreign_keys=[user_id])
