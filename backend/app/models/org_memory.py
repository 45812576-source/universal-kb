import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class OrgMemorySource(Base):
    __tablename__ = "org_memory_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    source_type = Column(String(50), nullable=False, default="markdown")
    source_uri = Column(String(1024), nullable=False)
    owner_name = Column(String(255), nullable=True)
    external_version = Column(String(100), nullable=True)
    fetched_at = Column(DateTime, nullable=True)
    ingest_status = Column(String(50), nullable=False, default="processing")
    latest_snapshot_id = Column(Integer, nullable=True)
    latest_snapshot_version = Column(String(100), nullable=True)
    latest_parse_note = Column(Text, nullable=True)
    bitable_app_token = Column(String(255), nullable=True)
    bitable_table_id = Column(String(255), nullable=True)
    raw_fields_json = Column(JSON, nullable=True)
    raw_records_json = Column(JSON, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    snapshots = relationship("OrgMemorySnapshot", back_populates="source", cascade="all, delete-orphan")


class OrgMemorySnapshot(Base):
    __tablename__ = "org_memory_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("org_memory_sources.id"), nullable=False)
    snapshot_version = Column(String(100), nullable=False)
    parse_status = Column(String(50), nullable=False, default="ready")
    confidence_score = Column(Float, nullable=False, default=0.0)
    summary = Column(Text, nullable=True)
    entity_counts_json = Column(JSON, default=dict)
    units_json = Column(JSON, default=list)
    roles_json = Column(JSON, default=list)
    people_json = Column(JSON, default=list)
    okrs_json = Column(JSON, default=list)
    processes_json = Column(JSON, default=list)
    low_confidence_items_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    source = relationship("OrgMemorySource", back_populates="snapshots")
    proposals = relationship("OrgMemoryProposal", back_populates="snapshot", cascade="all, delete-orphan")


class OrgMemoryProposal(Base):
    __tablename__ = "org_memory_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey("org_memory_snapshots.id"), nullable=False)
    title = Column(String(255), nullable=False)
    proposal_status = Column(String(50), nullable=False, default="draft")
    risk_level = Column(String(20), nullable=False, default="low")
    summary = Column(Text, nullable=True)
    impact_summary = Column(Text, nullable=True)
    structure_changes_json = Column(JSON, default=list)
    classification_rules_json = Column(JSON, default=list)
    skill_mounts_json = Column(JSON, default=list)
    approval_impacts_json = Column(JSON, default=list)
    evidence_refs_json = Column(JSON, default=list)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    snapshot = relationship("OrgMemorySnapshot", back_populates="proposals")
    applied_configs = relationship("OrgMemoryAppliedConfig", back_populates="proposal")
    config_versions = relationship("OrgMemoryConfigVersion", back_populates="proposal")


class OrgMemoryAppliedConfig(Base):
    __tablename__ = "org_memory_applied_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("org_memory_proposals.id"), nullable=False)
    approval_request_id = Column(Integer, ForeignKey("approval_requests.id"), nullable=True)
    status = Column(String(50), nullable=False, default="effective")
    applied_at = Column(DateTime, default=datetime.datetime.utcnow)
    knowledge_paths_json = Column(JSON, default=list)
    classification_rule_count = Column(Integer, default=0)
    skill_mount_count = Column(Integer, default=0)
    conditions_json = Column(JSON, default=list)

    proposal = relationship("OrgMemoryProposal", back_populates="applied_configs")


class OrgMemoryConfigVersion(Base):
    __tablename__ = "org_memory_config_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("org_memory_proposals.id"), nullable=False)
    applied_config_id = Column(Integer, ForeignKey("org_memory_applied_configs.id"), nullable=True)
    version = Column(Integer, nullable=False)
    action = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    applied_at = Column(DateTime, default=datetime.datetime.utcnow)
    knowledge_paths_json = Column(JSON, default=list)
    classification_rule_count = Column(Integer, default=0)
    skill_mount_count = Column(Integer, default=0)
    conditions_json = Column(JSON, default=list)
    note = Column(Text, nullable=True)

    proposal = relationship("OrgMemoryProposal", back_populates="config_versions")


class OrgMemoryApprovalLink(Base):
    __tablename__ = "org_memory_approval_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("org_memory_proposals.id"), nullable=False)
    approval_request_id = Column(Integer, ForeignKey("approval_requests.id"), nullable=False)
    external_approval_type = Column(String(100), nullable=False, default="internal_approval")
    external_status = Column(String(50), nullable=False, default="pending")
    last_synced_at = Column(DateTime, default=datetime.datetime.utcnow)
    callback_payload_json = Column(JSON, nullable=True)

    proposal = relationship("OrgMemoryProposal")
