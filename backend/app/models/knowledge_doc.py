"""协同文档模型：Tiptap + Yjs 实时协同编辑层。

KnowledgeDoc: 协同文档元信息
KnowledgeDocSnapshot: 文档快照（自动/手动/发布/导入）
KnowledgeDocComment: 块级评论与锚点
"""
import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON, LONGBLOB

from app.database import Base


class KnowledgeDoc(Base):
    """知识条目的协同文档层。一个 KnowledgeEntry 对应一个 KnowledgeDoc。"""
    __tablename__ = "knowledge_docs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, unique=True, index=True)
    doc_type = Column(String(30), default="cloud_doc", nullable=False)  # cloud_doc
    # Yjs 文档唯一标识（用作 WebSocket room key）
    yjs_doc_key = Column(String(200), nullable=False, unique=True)
    # Yjs 二进制状态（完整 Y.Doc state）
    yjs_state = Column(LONGBLOB, nullable=True)
    # Tiptap schema 版本号，用于未来 schema migration
    editor_schema_version = Column(Integer, default=1)
    current_snapshot_id = Column(Integer, nullable=True)
    # initializing | ready | degraded | failed
    collab_status = Column(String(20), default="initializing", nullable=False)
    collab_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class KnowledgeDocSnapshot(Base):
    """文档快照，用于历史恢复和版本比对。"""
    __tablename__ = "knowledge_doc_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    # autosave | manual | publish | import
    snapshot_type = Column(String(20), default="autosave", nullable=False)
    # Tiptap JSON 文档内容
    snapshot_json = Column(JSON, nullable=True)
    # Yjs 二进制 snapshot（可选，用于精确恢复）
    yjs_snapshot = Column(LONGBLOB, nullable=True)
    # 派生的纯文本摘要（前 500 字，用于快照列表预览）
    preview_text = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class KnowledgeDocComment(Base):
    """块级评论，锚定到文档中的 block 或文本区间。"""
    __tablename__ = "knowledge_doc_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    # 关联到 document_block 的 block_key
    block_key = Column(String(100), nullable=True)
    # 文本区间锚点（在 block 内的字符偏移）
    anchor_from = Column(Integer, nullable=True)
    anchor_to = Column(Integer, nullable=True)
    content = Column(Text, nullable=False)
    # open | resolved
    status = Column(String(20), default="open", nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
