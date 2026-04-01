"""add view_state and view_invalid_reason to table_views

Revision ID: a1b2c3d4e5f6
Revises: z7a8b9c0d1e3
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "z7a8b9c0d1e3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("table_views", sa.Column("view_state", sa.String(30), server_default="available", nullable=True))
    op.add_column("table_views", sa.Column("view_invalid_reason", sa.Text, nullable=True))


def downgrade():
    op.drop_column("table_views", "view_invalid_reason")
    op.drop_column("table_views", "view_state")
