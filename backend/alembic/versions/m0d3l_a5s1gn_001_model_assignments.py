"""调用点模型绑定表 model_assignments

Revision ID: m0d3l_a5s1gn_001
Revises: z7a8b9c0d1e2
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "m0d3l_a5s1gn_001"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_assignments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slot_key", sa.String(100), unique=True, nullable=False),
        sa.Column("model_config_id", sa.Integer, sa.ForeignKey("model_configs.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("model_assignments")
