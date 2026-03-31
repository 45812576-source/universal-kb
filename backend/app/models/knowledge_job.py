"""知识处理任务模型：render（云文档转换）和 classify（自动分类）的异步 Job。"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class KnowledgeJob(Base):
    __tablename__ = "knowledge_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    # render | classify
    job_type = Column(String(20), nullable=False, index=True)
    # queued | running | success | failed | partial_success
    status = Column(String(20), default="queued", nullable=False, index=True)
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    error_type = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    # queued | extracting | rendering | persisting | classifying
    phase = Column(String(30), nullable=True)
    # upload | retry | scheduled
    trigger_source = Column(String(20), default="upload")
    payload = Column(JSON, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
