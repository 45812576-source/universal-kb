"""知识文档块级模型：document_blocks（正文块）和 chunk_mappings（向量chunk↔block映射）。"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class KnowledgeDocumentBlock(Base):
    """云文档正文拆成的稳定 block，用于前端定位和检索跳块。"""
    __tablename__ = "knowledge_document_blocks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    block_key = Column(String(100), nullable=False)        # 稳定标识，前端锚点用
    block_type = Column(String(30), nullable=False)        # heading / paragraph / list / table / code / image
    block_order = Column(Integer, nullable=False, default=0)
    plain_text = Column(Text, nullable=True)               # 纯文本内容
    html_fragment = Column(Text, nullable=True)            # 原始 HTML 片段
    heading_path = Column(String(500), nullable=True)      # 所属标题路径，如 "一、背景 > 1.1 市场分析"
    start_offset = Column(Integer, nullable=True)          # 在 content 中的字符起始位置
    end_offset = Column(Integer, nullable=True)            # 在 content 中的字符结束位置
    source_anchor = Column(String(200), nullable=True)     # 可选的 HTML id/anchor
    # Yjs 文档内的路径标识（用于协同编辑时定位）
    yjs_path = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class KnowledgeChunkMapping(Base):
    """向量 chunk 与 document block 的映射关系。"""
    __tablename__ = "knowledge_chunk_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    milvus_chunk_id = Column(String(100), nullable=True)   # Milvus primary key
    block_id = Column(Integer, ForeignKey("knowledge_document_blocks.id"), nullable=True)
    block_key = Column(String(100), nullable=True)
    char_start_in_block = Column(Integer, nullable=True)
    char_end_in_block = Column(Integer, nullable=True)
    chunk_text = Column(Text, nullable=True)               # chunk 文本快照
    # 生成该 chunk 时的文档快照 ID（用于判断是否需要重建）
    snapshot_id = Column(Integer, ForeignKey("knowledge_doc_snapshots.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    block = relationship("KnowledgeDocumentBlock")
