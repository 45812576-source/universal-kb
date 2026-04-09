"""add last_verified_at to studio_instance_registrations

Revision ID: z7a8b9c0d2a2
Revises: z7a8b9c0d2a1
Create Date: 2026-04-09
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d2a2"
down_revision = "z7a8b9c0d2a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studio_instance_registrations",
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("studio_instance_registrations", "last_verified_at")
