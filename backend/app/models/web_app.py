import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class WebApp(Base):
    __tablename__ = "web_apps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    html_content = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_public = Column(Boolean, default=False)
    share_token = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String(50), default="draft", nullable=False)
    backend_cmd = Column(Text, nullable=True)
    backend_cwd = Column(Text, nullable=True)
    backend_port = Column(Integer, nullable=True)
    # 发布范围：company / dept / personal
    publish_scope = Column(String(20), default="personal", nullable=False)
    # 指定部门 id 列表（publish_scope=dept 时使用）
    publish_department_ids = Column(JSON, default=list)
    # 指定个人 user id 列表（publish_scope=personal 时使用）
    publish_user_ids = Column(JSON, default=list)

    creator = relationship("User", foreign_keys=[created_by])
