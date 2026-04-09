"""knowledge permission grants & permission change requests

Revision ID: z7a8b9c0d2a0
Revises: z7a8b9c0d1e9
Create Date: 2026-04-08

Changes:
1. CREATE TABLE knowledge_permission_grants  (细粒度知识资产权限)
2. CREATE TABLE permission_change_requests   (权限变更工单)
3. ALTER ENUM ApprovalRequestType ADD 'permission_change'
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d2a0"
down_revision = "z7a8b9c0d1e9"
branch_labels = None
depends_on = None


def upgrade():
    # 1. knowledge_permission_grants
    op.create_table(
        "knowledge_permission_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("grantee_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),     # folder | approval_capability
        sa.Column("resource_id", sa.Integer, nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(20), server_default="exact"),     # exact | subtree
        sa.Column("granted_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("granted_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("source", sa.String(20), server_default="direct"),   # direct | approval | role_default
    )
    op.create_index("ix_kpg_grantee", "knowledge_permission_grants", ["grantee_user_id"])
    op.create_index("ix_kpg_resource", "knowledge_permission_grants", ["resource_type", "resource_id"])

    # 2. permission_change_requests
    op.create_table(
        "permission_change_requests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("target_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("domain", sa.String(30), nullable=False),            # feature_flag | model_grant | knowledge_asset | approval_capability
        sa.Column("action_key", sa.String(100), nullable=False),
        sa.Column("current_value", sa.JSON, nullable=True),
        sa.Column("target_value", sa.JSON, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("risk_note", sa.Text, nullable=True),
        sa.Column("requester_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),  # pending | approved | rejected
        sa.Column("reviewer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("review_comment", sa.Text, nullable=True),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_pcr_target_user", "permission_change_requests", ["target_user_id"])
    op.create_index("ix_pcr_status", "permission_change_requests", ["status"])

    # 3. 扩展 approval_requests.request_type 枚举
    new_request_types = (
        "'skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review',"
        "'export_sensitive','elevate_disclosure','grant_access',"
        "'policy_change','field_sensitivity_change','small_sample_change',"
        "'permission_change'"
    )
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN request_type "
        f"ENUM({new_request_types}) NOT NULL"
    )


def downgrade():
    op.drop_table("permission_change_requests")
    op.drop_table("knowledge_permission_grants")
