import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class SkillServiceRole(Base):
    __tablename__ = "skill_service_roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, nullable=False, default=0)
    org_path = Column(String(512), nullable=False)
    division_name = Column(String(128), nullable=True)
    dept_level_1 = Column(String(128), nullable=True)
    dept_level_2 = Column(String(128), nullable=True)
    dept_level_3 = Column(String(128), nullable=True)
    position_name = Column(String(128), nullable=False)
    position_level = Column(String(64), nullable=True)
    role_label = Column(String(256), nullable=False)
    goal_summary = Column(Text, nullable=True)
    goal_refs_json = Column(JSON, default=list)
    source_dataset = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="active")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])

    __table_args__ = (
        UniqueConstraint("skill_id", "org_path", "position_name", "position_level", name="uq_skill_service_role"),
    )


class SkillBoundAsset(Base):
    __tablename__ = "skill_bound_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, nullable=False, default=0)
    asset_type = Column(String(32), nullable=False)
    asset_ref_type = Column(String(32), nullable=False)
    asset_ref_id = Column(Integer, nullable=False)
    asset_name = Column(String(256), nullable=False)
    binding_mode = Column(String(32), nullable=False)
    binding_scope_json = Column(JSON, default=dict)
    sensitivity_summary_json = Column(JSON, default=dict)
    risk_flags_json = Column(JSON, default=list)
    source_version = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])

    __table_args__ = (
        UniqueConstraint("skill_id", "asset_type", "asset_ref_type", "asset_ref_id", name="uq_skill_bound_asset"),
    )


class RolePolicyBundle(Base):
    __tablename__ = "role_policy_bundles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, nullable=False, default=0)
    bundle_version = Column(Integer, nullable=False)
    skill_content_version = Column(Integer, nullable=False, default=1)
    governance_version = Column(Integer, nullable=False, default=1)
    service_role_count = Column(Integer, nullable=False, default=0)
    bound_asset_count = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="draft")
    change_reason = Column(String(256), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    policies = relationship("RoleAssetPolicy", back_populates="bundle", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("skill_id", "bundle_version", name="uq_role_policy_bundle"),
    )


class RoleAssetPolicy(Base):
    __tablename__ = "role_asset_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bundle_id = Column(Integer, ForeignKey("role_policy_bundles.id", ondelete="CASCADE"), nullable=False)
    skill_service_role_id = Column(Integer, ForeignKey("skill_service_roles.id"), nullable=False)
    skill_bound_asset_id = Column(Integer, ForeignKey("skill_bound_assets.id"), nullable=False)
    allowed = Column(Boolean, nullable=False, default=True)
    default_output_style = Column(String(32), nullable=False)
    insufficient_evidence_behavior = Column(String(32), nullable=False, default="ask_clarification")
    allowed_question_types_json = Column(JSON, default=list)
    forbidden_question_types_json = Column(JSON, default=list)
    reason_basis_json = Column(JSON, default=list)
    policy_source = Column(String(32), nullable=False, default="system_suggested")
    review_status = Column(String(32), nullable=False, default="suggested")
    risk_level = Column(String(16), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    bundle = relationship("RolePolicyBundle", back_populates="policies")
    role = relationship("SkillServiceRole", foreign_keys=[skill_service_role_id])
    asset = relationship("SkillBoundAsset", foreign_keys=[skill_bound_asset_id])
    granular_rules = relationship("RoleAssetGranularRule", back_populates="policy", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("bundle_id", "skill_service_role_id", "skill_bound_asset_id", name="uq_role_asset_policy"),
    )


class RoleAssetGranularRule(Base):
    __tablename__ = "role_asset_granular_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_asset_policy_id = Column(Integer, ForeignKey("role_asset_policies.id", ondelete="CASCADE"), nullable=False)
    granularity_type = Column(String(16), nullable=False)
    target_ref = Column(String(255), nullable=False)
    target_class = Column(String(64), nullable=True)
    target_summary = Column(String(512), nullable=True)
    suggested_policy = Column(String(32), nullable=False)
    mask_style = Column(String(32), nullable=True)
    reason_basis_json = Column(JSON, default=list)
    confidence = Column(Integer, nullable=False, default=80)
    confirmed = Column(Boolean, nullable=False, default=False)
    author_override_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    policy = relationship("RoleAssetPolicy", back_populates="granular_rules")

    __table_args__ = (
        UniqueConstraint("role_asset_policy_id", "granularity_type", "target_ref", name="uq_role_asset_granular_rule"),
    )


class SkillRolePackage(Base):
    __tablename__ = "skill_role_packages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, nullable=False, default=0)
    skill_service_role_id = Column(Integer, ForeignKey("skill_service_roles.id", ondelete="SET NULL"), nullable=True)
    role_key = Column(String(768), nullable=False)
    org_path = Column(String(512), nullable=False)
    position_name = Column(String(128), nullable=False)
    position_level = Column(String(64), nullable=True)
    role_label = Column(String(256), nullable=False)
    package_version = Column(Integer, nullable=False, default=1)
    governance_version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="active")
    field_rules_json = Column(JSON, default=list)
    source_projection_version = Column(Integer, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    role = relationship("SkillServiceRole", foreign_keys=[skill_service_role_id])
    knowledge_overrides = relationship("SkillRoleKnowledgeOverride", back_populates="package", cascade="all, delete-orphan")
    asset_overrides = relationship("SkillRoleAssetMountOverride", back_populates="package", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_srp_skill_status", "skill_id", "status"),
    )


class SkillRoleKnowledgeOverride(Base):
    __tablename__ = "skill_role_knowledge_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    package_id = Column(Integer, ForeignKey("skill_role_packages.id", ondelete="CASCADE"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    role_key = Column(String(768), nullable=False)
    asset_id = Column(Integer, ForeignKey("skill_bound_assets.id", ondelete="CASCADE"), nullable=False)
    asset_ref = Column(String(128), nullable=False)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id", ondelete="CASCADE"), nullable=False)
    desensitization_level = Column(String(32), nullable=False, default="inherit")
    grant_actions_json = Column(JSON, default=list)
    enabled = Column(Boolean, nullable=False, default=True)
    source_refs_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    package = relationship("SkillRolePackage", back_populates="knowledge_overrides")
    asset = relationship("SkillBoundAsset", foreign_keys=[asset_id])

    __table_args__ = (
        Index("idx_srko_skill", "skill_id"),
    )


class SkillRoleAssetMountOverride(Base):
    __tablename__ = "skill_role_asset_mount_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    package_id = Column(Integer, ForeignKey("skill_role_packages.id", ondelete="CASCADE"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    role_key = Column(String(768), nullable=False)
    asset_id = Column(Integer, ForeignKey("skill_bound_assets.id", ondelete="CASCADE"), nullable=False)
    asset_ref_type = Column(String(32), nullable=False)
    asset_ref_id = Column(Integer, nullable=False)
    binding_mode = Column(String(32), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    package = relationship("SkillRolePackage", back_populates="asset_overrides")
    asset = relationship("SkillBoundAsset", foreign_keys=[asset_id])

    __table_args__ = (
        Index("idx_sramo_skill", "skill_id"),
    )


class PermissionDeclarationDraft(Base):
    __tablename__ = "permission_declaration_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    bundle_id = Column(Integer, ForeignKey("role_policy_bundles.id", ondelete="SET NULL"), nullable=True)
    role_policy_bundle_version = Column(Integer, nullable=False)
    governance_version = Column(Integer, nullable=False, default=1)
    generated_text = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="generated")
    source_refs_json = Column(JSON, default=list)
    diff_from_previous_json = Column(JSON, default=dict)
    edited_text = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    bundle = relationship("RolePolicyBundle", foreign_keys=[bundle_id])


class TestCasePlanDraft(Base):
    __tablename__ = "test_case_plan_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, nullable=False, default=0)
    bundle_id = Column(Integer, ForeignKey("role_policy_bundles.id", ondelete="SET NULL"), nullable=True)
    declaration_id = Column(Integer, ForeignKey("permission_declaration_drafts.id", ondelete="SET NULL"), nullable=True)
    plan_version = Column(Integer, nullable=False)
    skill_content_version = Column(Integer, nullable=False, default=1)
    governance_version = Column(Integer, nullable=False, default=1)
    permission_declaration_version = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False, default="generated")
    focus_mode = Column(String(32), nullable=False, default="risk_focused")
    max_cases = Column(Integer, nullable=False, default=12)
    case_count = Column(Integer, nullable=False, default=0)
    blocking_issues_json = Column(JSON, default=list)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    bundle = relationship("RolePolicyBundle", foreign_keys=[bundle_id])
    declaration = relationship("PermissionDeclarationDraft", foreign_keys=[declaration_id])
    cases = relationship("TestCaseDraft", back_populates="plan", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("skill_id", "plan_version", name="uq_test_case_plan_draft"),
    )


class TestCaseDraft(Base):
    __tablename__ = "test_case_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("test_case_plan_drafts.id", ondelete="CASCADE"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    target_role_ref = Column(Integer, ForeignKey("skill_service_roles.id"), nullable=False)
    role_label = Column(String(256), nullable=False)
    asset_ref = Column(String(128), nullable=False)
    asset_name = Column(String(256), nullable=False)
    asset_type = Column(String(32), nullable=False)
    case_type = Column(String(64), nullable=False)
    risk_tags_json = Column(JSON, default=list)
    prompt = Column(Text, nullable=False)
    expected_behavior = Column(Text, nullable=False)
    source_refs_json = Column(JSON, default=list)
    source_verification_status = Column(String(32), nullable=False, default="linked")
    data_source_policy = Column(String(32), nullable=False, default="verified_slot_only")
    status = Column(String(32), nullable=False, default="suggested")
    granular_refs_json = Column(JSON, default=list)
    controlled_fields_json = Column(JSON, default=list)
    edited_by_user = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    plan = relationship("TestCasePlanDraft", back_populates="cases")
    skill = relationship("Skill", foreign_keys=[skill_id])
    role = relationship("SkillServiceRole", foreign_keys=[target_role_ref])


class SandboxCaseMaterialization(Base):
    __tablename__ = "sandbox_case_materializations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("test_case_plan_drafts.id", ondelete="CASCADE"), nullable=False)
    case_draft_id = Column(Integer, ForeignKey("test_case_drafts.id", ondelete="CASCADE"), nullable=False)
    sandbox_session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id", ondelete="CASCADE"), nullable=False)
    sandbox_case_id = Column(Integer, ForeignKey("sandbox_test_cases.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(32), nullable=False, default="materialized")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    plan = relationship("TestCasePlanDraft", foreign_keys=[plan_id])
    case_draft = relationship("TestCaseDraft", foreign_keys=[case_draft_id])

    __table_args__ = (
        UniqueConstraint("case_draft_id", "sandbox_session_id", name="uq_sandbox_case_materialization"),
    )


class SkillGovernanceJob(Base):
    __tablename__ = "skill_governance_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = Column(Integer, nullable=False, default=0)
    job_type = Column(String(32), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    phase = Column(String(64), nullable=True)
    payload_json = Column(JSON, default=dict)
    result_json = Column(JSON, default=dict)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
