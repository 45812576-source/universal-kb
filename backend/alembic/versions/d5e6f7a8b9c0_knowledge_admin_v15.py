"""knowledge admin v1.5: folder grants, audit logs, rerun jobs

Revision ID: d5e6f7a8b9c0
Revises: b3c4d5e6f7g8
Create Date: 2026-04-02

Changes:
1. CREATE TABLE knowledge_folder_grants
2. CREATE TABLE knowledge_folder_audit_logs
3. CREATE TABLE knowledge_rerun_jobs
4. ALTER TABLE knowledge_entries ADD system_title_prefix, manual_title_locked
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "b3c4d5e6f7g8"
branch_labels = None
depends_on = None


def upgrade():
    # 1. knowledge_folder_grants
    op.create_table(
        "knowledge_folder_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer, sa.ForeignKey("knowledge_folders.id"), nullable=False),
        sa.Column("grantee_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(20), server_default="subtree"),
        sa.Column("can_manage_children", sa.Boolean, server_default=sa.text("1")),
        sa.Column("can_delete_descendants", sa.Boolean, server_default=sa.text("1")),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("folder_id", "grantee_user_id", name="uq_folder_grant"),
    )

    # 2. knowledge_folder_audit_logs
    op.create_table(
        "knowledge_folder_audit_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer, sa.ForeignKey("knowledge_folders.id"), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("old_value", sa.JSON, nullable=True),
        sa.Column("new_value", sa.JSON, nullable=True),
        sa.Column("performed_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_folder_audit_folder_id", "knowledge_folder_audit_logs", ["folder_id"])

    # 3. knowledge_rerun_jobs
    op.create_table(
        "knowledge_rerun_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trigger_type", sa.String(30), nullable=False),
        sa.Column("target_folder_id", sa.Integer, sa.ForeignKey("knowledge_folders.id"), nullable=False),
        sa.Column("target_scope", sa.String(20), server_default="subtree"),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("affected_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("reclassified_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("renamed_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("skipped_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("error_log", sa.Text, nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_rerun_jobs_status", "knowledge_rerun_jobs", ["status"])

    # 4. knowledge_entries 新增字段
    op.add_column(
        "knowledge_entries",
        sa.Column("system_title_prefix", sa.String(100), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("manual_title_locked", sa.Boolean, server_default=sa.text("0")),
    )


def downgrade():
    op.drop_column("knowledge_entries", "manual_title_locked")
    op.drop_column("knowledge_entries", "system_title_prefix")
    op.drop_index("ix_rerun_jobs_status", "knowledge_rerun_jobs")
    op.drop_table("knowledge_rerun_jobs")
    op.drop_index("ix_folder_audit_folder_id", "knowledge_folder_audit_logs")
    op.drop_table("knowledge_folder_audit_logs")
    op.drop_table("knowledge_folder_grants")
