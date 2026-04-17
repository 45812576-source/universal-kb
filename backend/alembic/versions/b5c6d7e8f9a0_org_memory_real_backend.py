"""org memory real backend tables

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    request_types = (
        "'skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review',"
        "'export_sensitive','elevate_disclosure','grant_access',"
        "'policy_change','field_sensitivity_change','small_sample_change',"
        "'permission_change','org_memory_proposal','knowledge_scope_expand',"
        "'knowledge_redaction_lower','skill_mount_org_memory'"
    )
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN request_type "
        f"ENUM({request_types}) NOT NULL"
    )

    op.create_table(
        "org_memory_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False, server_default="markdown"),
        sa.Column("source_uri", sa.String(1024), nullable=False),
        sa.Column("owner_name", sa.String(255), nullable=True),
        sa.Column("external_version", sa.String(100), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("ingest_status", sa.String(50), nullable=False, server_default="processing"),
        sa.Column("latest_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("latest_snapshot_version", sa.String(100), nullable=True),
        sa.Column("latest_parse_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_org_memory_sources_status", "org_memory_sources", ["ingest_status"])

    op.create_table(
        "org_memory_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("org_memory_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_version", sa.String(100), nullable=False),
        sa.Column("parse_status", sa.String(50), nullable=False, server_default="ready"),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("entity_counts_json", mysql.JSON(), nullable=True),
        sa.Column("units_json", mysql.JSON(), nullable=True),
        sa.Column("roles_json", mysql.JSON(), nullable=True),
        sa.Column("people_json", mysql.JSON(), nullable=True),
        sa.Column("okrs_json", mysql.JSON(), nullable=True),
        sa.Column("processes_json", mysql.JSON(), nullable=True),
        sa.Column("low_confidence_items_json", mysql.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_org_memory_snapshots_source", "org_memory_snapshots", ["source_id"])

    op.create_table(
        "org_memory_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("org_memory_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("proposal_status", sa.String(50), nullable=False, server_default="draft"),
        sa.Column("risk_level", sa.String(20), nullable=False, server_default="low"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("impact_summary", sa.Text(), nullable=True),
        sa.Column("structure_changes_json", mysql.JSON(), nullable=True),
        sa.Column("classification_rules_json", mysql.JSON(), nullable=True),
        sa.Column("skill_mounts_json", mysql.JSON(), nullable=True),
        sa.Column("approval_impacts_json", mysql.JSON(), nullable=True),
        sa.Column("evidence_refs_json", mysql.JSON(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_org_memory_proposals_snapshot", "org_memory_proposals", ["snapshot_id"])
    op.create_index("idx_org_memory_proposals_status", "org_memory_proposals", ["proposal_status"])

    op.create_table(
        "org_memory_applied_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("org_memory_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approval_request_id", sa.Integer(), sa.ForeignKey("approval_requests.id"), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="effective"),
        sa.Column("applied_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("knowledge_paths_json", mysql.JSON(), nullable=True),
        sa.Column("classification_rule_count", sa.Integer(), server_default="0"),
        sa.Column("skill_mount_count", sa.Integer(), server_default="0"),
        sa.Column("conditions_json", mysql.JSON(), nullable=True),
    )
    op.create_index("idx_org_memory_applied_configs_proposal", "org_memory_applied_configs", ["proposal_id"])

    op.create_table(
        "org_memory_config_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("org_memory_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("applied_config_id", sa.Integer(), sa.ForeignKey("org_memory_applied_configs.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("applied_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("knowledge_paths_json", mysql.JSON(), nullable=True),
        sa.Column("classification_rule_count", sa.Integer(), server_default="0"),
        sa.Column("skill_mount_count", sa.Integer(), server_default="0"),
        sa.Column("conditions_json", mysql.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("idx_org_memory_config_versions_proposal", "org_memory_config_versions", ["proposal_id", "version"])

    op.create_table(
        "org_memory_approval_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("org_memory_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approval_request_id", sa.Integer(), sa.ForeignKey("approval_requests.id"), nullable=False),
        sa.Column("external_approval_type", sa.String(100), nullable=False, server_default="internal_approval"),
        sa.Column("external_status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("last_synced_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("callback_payload_json", mysql.JSON(), nullable=True),
    )
    op.create_index("idx_org_memory_approval_links_proposal", "org_memory_approval_links", ["proposal_id"])
    op.create_index("idx_org_memory_approval_links_request", "org_memory_approval_links", ["approval_request_id"])


def downgrade() -> None:
    op.drop_table("org_memory_approval_links")
    op.drop_table("org_memory_config_versions")
    op.drop_table("org_memory_applied_configs")
    op.drop_table("org_memory_proposals")
    op.drop_table("org_memory_snapshots")
    op.drop_table("org_memory_sources")

    request_types = (
        "'skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review',"
        "'export_sensitive','elevate_disclosure','grant_access',"
        "'policy_change','field_sensitivity_change','small_sample_change',"
        "'permission_change'"
    )
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN request_type "
        f"ENUM({request_types}) NOT NULL"
    )
