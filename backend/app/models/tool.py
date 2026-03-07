import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class ToolType(str, enum.Enum):
    MCP = "mcp"
    BUILTIN = "builtin"
    HTTP = "http"


class ToolRegistry(Base):
    __tablename__ = "tool_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    tool_type = Column(Enum(ToolType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    config = Column(JSON, default=dict)           # MCP: {command, args, env} / HTTP: {url, method, headers}
    input_schema = Column(JSON, default=dict)      # JSON Schema for tool parameters
    output_format = Column(String(50), default="json")  # json / file / text
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    skills = relationship("Skill", secondary="skill_tools", back_populates="bound_tools")


class SkillTool(Base):
    __tablename__ = "skill_tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=False)
