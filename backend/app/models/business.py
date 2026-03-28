import datetime
import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class VisibilityLevel(str, enum.Enum):
    DETAIL = "detail"
    DESENSITIZED = "desensitized"
    STATS = "stats"


class DataOwnership(Base):
    __tablename__ = "data_ownership_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(100), nullable=False)
    owner_field = Column(String(100), nullable=False)  # e.g. "sales_rep_id"
    department_field = Column(String(100), nullable=True)  # e.g. "department_id"
    visibility_level = Column(
        Enum(VisibilityLevel, values_callable=lambda obj: [e.value for e in obj]),
        default=VisibilityLevel.DETAIL,
    )
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class BusinessTable(Base):
    __tablename__ = "business_tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ddl_sql = Column(Text)
    validation_rules = Column(JSON, default=dict)
    workflow = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    owner = relationship("User", foreign_keys=[owner_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    table_name = Column(String(100), nullable=False)
    operation = Column(String(20), nullable=False)  # INSERT/UPDATE/DELETE
    row_id = Column(String(100))
    old_values = Column(JSON)
    new_values = Column(JSON)
    sql_executed = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SkillDataQuery(Base):
    __tablename__ = "skill_data_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    query_name = Column(String(100), nullable=False)
    query_type = Column(String(20), nullable=False)  # read/write/compute
    table_name = Column(String(100), nullable=False)
    description = Column(Text)
    template_sql = Column(Text)

    skill = relationship("Skill", foreign_keys=[skill_id])


class TableView(Base):
    """User-defined views for a business table: saved filter/sort/group/column config."""
    __tablename__ = "table_views"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    view_type = Column(String(20), default="grid")   # "grid" | "kanban" | "gallery"
    config = Column(JSON, default=dict)              # {filters, sorts, group_by, hidden_columns, column_widths}
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    table = relationship("BusinessTable", foreign_keys=[table_id])
