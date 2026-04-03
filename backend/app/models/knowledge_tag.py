"""知识标签治理模型

- KnowledgeTag: 标签主数据（支持层级）
- KnowledgeTagRelation: 标签间语义关系
"""
import datetime
import enum

from sqlalchemy import (
    Column, DateTime, Enum, Float, Integer, String, Text,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class TagCategory(str, enum.Enum):
    """标签大类"""
    INDUSTRY = "industry"       # 行业
    PLATFORM = "platform"       # 平台
    TOPIC = "topic"             # 主题
    SCENARIO = "scenario"       # 场景
    CUSTOM = "custom"           # 自定义


class TagRelationType(str, enum.Enum):
    """语义关系类型"""
    SYNONYM = "synonym"         # 同义词（双向）
    BROADER = "broader"         # 上位词（A broader B → A 是 B 的上位）
    NARROWER = "narrower"       # 下位词（A narrower B → A 是 B 的下位）
    RELATED = "related"         # 相关（双向）


class KnowledgeTag(Base):
    """标签主数据，支持层级树。"""
    __tablename__ = "knowledge_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    code = Column(String(50), nullable=False, unique=True)     # 唯一编码，如 "ind_food"
    category = Column(
        Enum(TagCategory, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    parent_id = Column(Integer, ForeignKey("knowledge_tags.id"), nullable=True)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Integer, default=1)                     # 1=启用 0=停用
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    children = relationship(
        "KnowledgeTag",
        back_populates="parent_tag",
        cascade="all, delete-orphan",
        order_by="KnowledgeTag.sort_order",
    )
    parent_tag = relationship("KnowledgeTag", back_populates="children", remote_side=[id])


class KnowledgeTagRelation(Base):
    """标签间语义关系。

    关系类型：
    - synonym: A 和 B 同义（双向，只需存一条）
    - broader: source 是 target 的上位
    - narrower: source 是 target 的下位
    - related: A 和 B 相关（双向，只需存一条）
    """
    __tablename__ = "knowledge_tag_relations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_tag_id = Column(Integer, ForeignKey("knowledge_tags.id"), nullable=False)
    target_tag_id = Column(Integer, ForeignKey("knowledge_tags.id"), nullable=False)
    relation_type = Column(
        Enum(TagRelationType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    confidence = Column(Float, default=1.0)    # 关系置信度 0-1
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    source_tag = relationship("KnowledgeTag", foreign_keys=[source_tag_id])
    target_tag = relationship("KnowledgeTag", foreign_keys=[target_tag_id])

    __table_args__ = (
        UniqueConstraint("source_tag_id", "target_tag_id", "relation_type", name="uq_tag_relation"),
    )
