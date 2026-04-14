"""add publish_status to business_tables

Revision ID: z7a8b9c0d2a6
Revises: z7a8b9c0d2a5
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d2a6"
down_revision = ("z7a8b9c0d2a4", "z7a8b9c0d2a5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("business_tables", sa.Column("publish_status", sa.String(20), server_default="draft", nullable=True))
    op.add_column("business_tables", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column("business_tables", sa.Column("published_by", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("business_tables", "published_by")
    op.drop_column("business_tables", "published_at")
    op.drop_column("business_tables", "publish_status")
