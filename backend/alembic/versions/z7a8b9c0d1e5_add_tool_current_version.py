"""add current_version to tool_registry

Revision ID: z7a8b9c0d1e5
Revises: z7a8b9c0d1e4
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e5"
down_revision = "z7a8b9c0d1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='tool_registry' AND column_name='current_version'"
    ))
    if result.scalar() == 0:
        op.add_column(
            "tool_registry",
            sa.Column("current_version", sa.Integer(), nullable=True, server_default="1"),
        )


def downgrade() -> None:
    op.drop_column("tool_registry", "current_version")
