"""归档建议模型：AI 自动推荐 folder 和分类路径。"""
import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class KnowledgeFilingSuggestion(Base):
    """知识条目的自动归档建议。"""
    __tablename__ = "knowledge_filing_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    suggested_folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    suggested_folder_path = Column(String(500), nullable=True)   # "广告投放/抖音"
    confidence = Column(Float, default=0.0)
    reason = Column(Text, nullable=True)                         # AI 推荐理由
    based_on = Column(JSON, nullable=True)                       # {"taxonomy": ..., "similar_entries": [...], ...}
    # pending | accepted | rejected
    status = Column(String(20), default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
