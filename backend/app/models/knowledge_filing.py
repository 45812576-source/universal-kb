"""归档模型：建议 + 操作审计。"""
import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class KnowledgeFilingSuggestion(Base):
    """知识条目的自动归档建议（v1 legacy，仍被批量建议接口使用）。"""
    __tablename__ = "knowledge_filing_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    suggested_folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    suggested_folder_path = Column(String(500), nullable=True)
    confidence = Column(Float, default=0.0)
    reason = Column(Text, nullable=True)
    based_on = Column(JSON, nullable=True)
    # pending | accepted | rejected
    status = Column(String(20), default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class KnowledgeFilingAction(Base):
    """归档操作审计记录。每次自动/手动归档和撤销都记录一条。"""
    __tablename__ = "knowledge_filing_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    # auto_file | manual_move | undo_auto_file | batch_auto_file
    action_type = Column(String(30), nullable=False)
    from_folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    to_folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    # taxonomy | vector_neighbors | rule | manual
    decision_source = Column(String(30), nullable=True)
    confidence = Column(Float, nullable=True)
    reason = Column(Text, nullable=True)
    # batch_id: 同一批次的自动归档共享一个 batch_id，用于批量撤销
    batch_id = Column(String(50), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
