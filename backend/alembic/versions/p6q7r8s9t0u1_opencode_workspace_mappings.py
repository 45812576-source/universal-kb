"""opencode workspace mappings

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-03-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "p6q7r8s9t0u1"
down_revision: Union[str, None] = "o5p6q7r8s9t0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opencode_workspace_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("opencode_workspace_id", sa.String(255), nullable=False, unique=True),
        sa.Column("opencode_workspace_name", sa.String(255), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "opencode_usage_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lines_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lines_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("models", sa.JSON(), nullable=True),
        sa.Column("workspaces", sa.JSON(), nullable=True),
        sa.Column("output_files", sa.JSON(), nullable=True),
        sa.Column("skills_submitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tools_submitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("opencode_usage_cache")
    op.drop_table("opencode_workspace_mappings")
