"""skill role package writeback tables

Revision ID: e7f8g9h0i1j2
Revises: d6e7f8g9h0i1
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa


revision = "e7f8g9h0i1j2"
down_revision = "d6e7f8g9h0i1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_role_packages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skill_service_role_id", sa.Integer(), sa.ForeignKey("skill_service_roles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("role_key", sa.String(768), nullable=False),
        sa.Column("org_path", sa.String(512), nullable=False),
        sa.Column("position_name", sa.String(128), nullable=False),
        sa.Column("position_level", sa.String(64), nullable=True),
        sa.Column("role_label", sa.String(256), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("governance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("field_rules_json", sa.JSON(), nullable=True),
        sa.Column("source_projection_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_srp_skill_status", "skill_role_packages", ["skill_id", "status"])

    op.create_table(
        "skill_role_knowledge_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("skill_role_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_key", sa.String(768), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("skill_bound_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_ref", sa.String(128), nullable=False),
        sa.Column("knowledge_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("desensitization_level", sa.String(32), nullable=False, server_default="inherit"),
        sa.Column("grant_actions_json", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_refs_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_srko_skill", "skill_role_knowledge_overrides", ["skill_id"])

    op.create_table(
        "skill_role_asset_mount_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("skill_role_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_key", sa.String(768), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("skill_bound_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_ref_type", sa.String(32), nullable=False),
        sa.Column("asset_ref_id", sa.Integer(), nullable=False),
        sa.Column("binding_mode", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sramo_skill", "skill_role_asset_mount_overrides", ["skill_id"])


def downgrade() -> None:
    op.drop_index("idx_sramo_skill", table_name="skill_role_asset_mount_overrides")
    op.drop_table("skill_role_asset_mount_overrides")
    op.drop_index("idx_srko_skill", table_name="skill_role_knowledge_overrides")
    op.drop_table("skill_role_knowledge_overrides")
    op.drop_index("idx_srp_skill_status", table_name="skill_role_packages")
    op.drop_table("skill_role_packages")
