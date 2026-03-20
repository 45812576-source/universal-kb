"""add tool_publish to approval_requests request_type enum

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'r8s9t0u1v2w3'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE approval_requests MODIFY COLUMN request_type "
        "ENUM('skill_publish','tool_publish','scope_change','mask_override','schema_approval') NOT NULL"
    )


def downgrade():
    op.execute(
        "ALTER TABLE approval_requests MODIFY COLUMN request_type "
        "ENUM('skill_publish','scope_change','mask_override','schema_approval') NOT NULL"
    )
