"""add_studio_registrations

Revision ID: c3d4e5f6a7b9
Revises: z7a8b9c0d2a1
Create Date: 2026-04-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, Sequence[str], None] = "z7a8b9c0d2a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "studio_instance_registrations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workspace_type", sa.String(20), nullable=False),
        sa.Column("workspace_root", sa.String(1024), nullable=False),
        sa.Column("project_dir", sa.String(1024), nullable=False),
        sa.Column("primary_conversation_id", sa.Integer(), nullable=True),
        sa.Column("runtime_port", sa.Integer(), nullable=True),
        sa.Column("runtime_status", sa.String(20), server_default="stopped"),
        sa.Column("generation", sa.Integer(), server_default="0"),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        sa.Column("last_recovered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["primary_conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "workspace_type", name="uq_user_workspace_type"),
    )


def downgrade() -> None:
    op.drop_table("studio_instance_registrations")
