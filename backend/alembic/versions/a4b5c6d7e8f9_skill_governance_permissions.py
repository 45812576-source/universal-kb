"""skill governance permission assistant tables

Revision ID: a4b5c6d7e8f9
Revises: z7a8b9c0d2a7
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "z7a8b9c0d2a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_service_roles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("org_path", sa.String(512), nullable=False),
        sa.Column("division_name", sa.String(128), nullable=True),
        sa.Column("dept_level_1", sa.String(128), nullable=True),
        sa.Column("dept_level_2", sa.String(128), nullable=True),
        sa.Column("dept_level_3", sa.String(128), nullable=True),
        sa.Column("position_name", sa.String(128), nullable=False),
        sa.Column("position_level", sa.String(64), nullable=True),
        sa.Column("role_label", sa.String(256), nullable=False),
        sa.Column("goal_summary", sa.Text(), nullable=True),
        sa.Column("goal_refs_json", sa.JSON(), nullable=True),
        sa.Column("source_dataset", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "org_path", "position_name", "position_level", name="uq_skill_service_role"),
    )
    op.create_index("idx_ssr_skill_status", "skill_service_roles", ["skill_id", "status"])
    op.create_index("idx_ssr_workspace_skill", "skill_service_roles", ["workspace_id", "skill_id"])

    op.create_table(
        "skill_bound_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("asset_ref_type", sa.String(32), nullable=False),
        sa.Column("asset_ref_id", sa.Integer(), nullable=False),
        sa.Column("asset_name", sa.String(256), nullable=False),
        sa.Column("binding_mode", sa.String(32), nullable=False),
        sa.Column("binding_scope_json", sa.JSON(), nullable=True),
        sa.Column("sensitivity_summary_json", sa.JSON(), nullable=True),
        sa.Column("risk_flags_json", sa.JSON(), nullable=True),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "asset_type", "asset_ref_type", "asset_ref_id", name="uq_skill_bound_asset"),
    )
    op.create_index("idx_sba_skill_type_status", "skill_bound_assets", ["skill_id", "asset_type", "status"])

    op.create_table(
        "role_policy_bundles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bundle_version", sa.Integer(), nullable=False),
        sa.Column("skill_content_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("governance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("service_role_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bound_asset_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("change_reason", sa.String(256), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "bundle_version", name="uq_role_policy_bundle"),
    )
    op.create_index("idx_rpb_skill_status_created", "role_policy_bundles", ["skill_id", "status", "created_at"])

    op.create_table(
        "role_asset_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bundle_id", sa.Integer(), sa.ForeignKey("role_policy_bundles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_service_role_id", sa.Integer(), sa.ForeignKey("skill_service_roles.id"), nullable=False),
        sa.Column("skill_bound_asset_id", sa.Integer(), sa.ForeignKey("skill_bound_assets.id"), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_output_style", sa.String(32), nullable=False),
        sa.Column("insufficient_evidence_behavior", sa.String(32), nullable=False),
        sa.Column("allowed_question_types_json", sa.JSON(), nullable=True),
        sa.Column("forbidden_question_types_json", sa.JSON(), nullable=True),
        sa.Column("reason_basis_json", sa.JSON(), nullable=True),
        sa.Column("policy_source", sa.String(32), nullable=False, server_default="system_suggested"),
        sa.Column("review_status", sa.String(32), nullable=False, server_default="suggested"),
        sa.Column("risk_level", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bundle_id", "skill_service_role_id", "skill_bound_asset_id", name="uq_role_asset_policy"),
    )
    op.create_index("idx_rap_bundle_review_risk", "role_asset_policies", ["bundle_id", "review_status", "risk_level"])
    op.create_index("idx_rap_role_asset", "role_asset_policies", ["skill_service_role_id", "skill_bound_asset_id"])

    op.create_table(
        "role_asset_granular_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("role_asset_policy_id", sa.Integer(), sa.ForeignKey("role_asset_policies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("granularity_type", sa.String(16), nullable=False),
        sa.Column("target_ref", sa.String(255), nullable=False),
        sa.Column("target_class", sa.String(64), nullable=True),
        sa.Column("target_summary", sa.String(512), nullable=True),
        sa.Column("suggested_policy", sa.String(32), nullable=False),
        sa.Column("mask_style", sa.String(32), nullable=True),
        sa.Column("reason_basis_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("author_override_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_asset_policy_id", "granularity_type", "target_ref", name="uq_role_asset_granular_rule"),
    )
    op.create_index("idx_ragr_policy_type", "role_asset_granular_rules", ["role_asset_policy_id", "granularity_type"])

    op.create_table(
        "permission_declaration_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bundle_id", sa.Integer(), sa.ForeignKey("role_policy_bundles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("role_policy_bundle_version", sa.Integer(), nullable=False),
        sa.Column("governance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="generated"),
        sa.Column("source_refs_json", sa.JSON(), nullable=True),
        sa.Column("diff_from_previous_json", sa.JSON(), nullable=True),
        sa.Column("edited_text", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_pdd_skill_status_created", "permission_declaration_drafts", ["skill_id", "status", "created_at"])

    op.create_table(
        "test_case_plan_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bundle_id", sa.Integer(), sa.ForeignKey("role_policy_bundles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("declaration_id", sa.Integer(), sa.ForeignKey("permission_declaration_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("skill_content_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("governance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("permission_declaration_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="generated"),
        sa.Column("focus_mode", sa.String(32), nullable=False, server_default="risk_focused"),
        sa.Column("max_cases", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocking_issues_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "plan_version", name="uq_test_case_plan_draft"),
    )
    op.create_index("idx_tcpd_skill_status_created", "test_case_plan_drafts", ["skill_id", "status", "created_at"])

    op.create_table(
        "test_case_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("test_case_plan_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_role_ref", sa.Integer(), sa.ForeignKey("skill_service_roles.id"), nullable=False),
        sa.Column("role_label", sa.String(256), nullable=False),
        sa.Column("asset_ref", sa.String(128), nullable=False),
        sa.Column("asset_name", sa.String(256), nullable=False),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("case_type", sa.String(64), nullable=False),
        sa.Column("risk_tags_json", sa.JSON(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("expected_behavior", sa.Text(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=True),
        sa.Column("source_verification_status", sa.String(32), nullable=False, server_default="linked"),
        sa.Column("data_source_policy", sa.String(32), nullable=False, server_default="verified_slot_only"),
        sa.Column("status", sa.String(32), nullable=False, server_default="suggested"),
        sa.Column("granular_refs_json", sa.JSON(), nullable=True),
        sa.Column("controlled_fields_json", sa.JSON(), nullable=True),
        sa.Column("edited_by_user", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tcd_plan_status", "test_case_drafts", ["plan_id", "status"])

    op.create_table(
        "sandbox_case_materializations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("test_case_plan_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_draft_id", sa.Integer(), sa.ForeignKey("test_case_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sandbox_session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sandbox_case_id", sa.Integer(), sa.ForeignKey("sandbox_test_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="materialized"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_draft_id", "sandbox_session_id", name="uq_sandbox_case_materialization"),
    )
    op.create_index("idx_scm_skill_plan", "sandbox_case_materializations", ["skill_id", "plan_id"])


def downgrade() -> None:
    op.drop_index("idx_scm_skill_plan", table_name="sandbox_case_materializations")
    op.drop_table("sandbox_case_materializations")
    op.drop_index("idx_tcd_plan_status", table_name="test_case_drafts")
    op.drop_table("test_case_drafts")
    op.drop_index("idx_tcpd_skill_status_created", table_name="test_case_plan_drafts")
    op.drop_table("test_case_plan_drafts")
    op.drop_index("idx_pdd_skill_status_created", table_name="permission_declaration_drafts")
    op.drop_table("permission_declaration_drafts")
    op.drop_index("idx_ragr_policy_type", table_name="role_asset_granular_rules")
    op.drop_table("role_asset_granular_rules")
    op.drop_index("idx_rap_role_asset", table_name="role_asset_policies")
    op.drop_index("idx_rap_bundle_review_risk", table_name="role_asset_policies")
    op.drop_table("role_asset_policies")
    op.drop_index("idx_rpb_skill_status_created", table_name="role_policy_bundles")
    op.drop_table("role_policy_bundles")
    op.drop_index("idx_sba_skill_type_status", table_name="skill_bound_assets")
    op.drop_table("skill_bound_assets")
    op.drop_index("idx_ssr_workspace_skill", table_name="skill_service_roles")
    op.drop_index("idx_ssr_skill_status", table_name="skill_service_roles")
    op.drop_table("skill_service_roles")
