"""user_capability_grants table

Revision ID: z7a8b9c0d2a1
Revises: z7a8b9c0d2a0
Create Date: 2026-04-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "z7a8b9c0d2a1"
down_revision = "b1c2d3e4f5g6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_capability_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("capability_key", sa.String(100), nullable=False),
        sa.Column("granted_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("granted_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("source", sa.String(20), server_default="direct"),
        sa.Column("scope_json", JSON, nullable=True),
        sa.UniqueConstraint("user_id", "capability_key", name="uq_user_capability"),
    )


def downgrade() -> None:
    op.drop_table("user_capability_grants")
