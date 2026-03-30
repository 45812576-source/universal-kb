"""add skill_memos table

Revision ID: a1b2c3d4e5f7
Revises: z7a8b9c0d1e2
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f7"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_memos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("scenario_type", sa.String(40), nullable=False),
        sa.Column("lifecycle_stage", sa.String(40), nullable=False, server_default="analysis"),
        sa.Column("status_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("goal_summary", sa.Text(), nullable=True),
        sa.Column("memo_payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_context_rollup", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id"),
    )


def downgrade() -> None:
    op.drop_table("skill_memos")
