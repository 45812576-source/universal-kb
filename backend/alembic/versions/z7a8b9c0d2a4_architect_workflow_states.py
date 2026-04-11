"""add architect_workflow_states table

Revision ID: z7a8b9c0d2a4
Revises: z7a8b9c0d2a3
Create Date: 2026-04-11 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "z7a8b9c0d2a4"
down_revision = "z7a8b9c0d2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architect_workflow_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.Integer(), nullable=True),
        sa.Column("workflow_mode", sa.String(30), server_default="architect_mode", nullable=True),
        sa.Column("workflow_phase", sa.String(30), server_default="phase_1_why", nullable=True),
        sa.Column("phase_outputs", JSON, nullable=True),
        sa.Column("ooda_round", sa.Integer(), server_default="0", nullable=True),
        sa.Column("phase_confirmed", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"]),
        sa.UniqueConstraint("conversation_id"),
    )


def downgrade() -> None:
    op.drop_table("architect_workflow_states")
