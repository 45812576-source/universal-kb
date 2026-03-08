import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON

from app.database import Base


class SkillMaster(Base):
    __tablename__ = "skill_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_code = Column(String(50), unique=True, nullable=False)
    skill_name = Column(String(200), nullable=False)
    priority = Column(String(20), nullable=False)
    main_chain = Column(String(100), nullable=True)
    core_scenario = Column(Text, nullable=True)
    primary_departments = Column(JSON, default=list)
    primary_roles = Column(JSON, default=list)
    low_friction_input = Column(JSON, default=list)
    system_inputs = Column(JSON, default=list)
    system_outputs = Column(JSON, default=list)
    artifact_type = Column(String(100), nullable=True)
    knowledge_layers = Column(JSON, default=list)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class InputTaxonomy(Base):
    __tablename__ = "input_taxonomy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    taxonomy_code = Column(String(50), unique=True, nullable=False)
    level_1_business_object = Column(String(100), nullable=False)
    level_2_evidence_purpose = Column(String(100), nullable=False)
    level_3_storage_form = Column(String(50), nullable=False)
    level_4_system_stage = Column(String(50), nullable=False)
    category_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    typical_examples = Column(JSON, default=list)
    supported_input_actions = Column(JSON, default=list)
    target_objects = Column(JSON, default=list)
    default_artifact_types = Column(JSON, default=list)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ObjectFieldDictionary(Base):
    __tablename__ = "object_field_dictionary"
    __table_args__ = (
        UniqueConstraint("object_type", "field_name", name="uq_object_field_dictionary_object_field"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    field_group = Column(String(100), nullable=False)
    object_type = Column(String(100), nullable=False)
    field_name = Column(String(100), nullable=False)
    field_label = Column(String(200), nullable=False)
    field_type = Column(String(50), nullable=False)
    field_description = Column(Text, nullable=True)
    source_layer = Column(String(100), nullable=True)
    source_method = Column(String(200), nullable=True)
    confirmation_mode = Column(String(50), nullable=True)
    storage_layer = Column(String(50), nullable=True)
    example_values = Column(JSON, default=list)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class Confirmation(Base):
    __tablename__ = "confirmations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=False)
    field_name = Column(String(100), nullable=False)
    question = Column(Text, nullable=False)
    question_type = Column(String(50), nullable=False, default="single_choice")
    options_json = Column(JSON, default=list)
    suggested_value = Column(Text, nullable=True)
    confirmed_value = Column(Text, nullable=True)
    status = Column(String(50), default="pending")
    confidence = Column(String(20), nullable=True)
    confirmed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
