"""sandbox v2: step_statuses, parent_session_id, rerun_scope

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # sandbox_test_sessions 新增字段
    op.add_column(
        "sandbox_test_sessions",
        sa.Column("step_statuses", mysql.JSON(), nullable=True),
    )
    op.add_column(
        "sandbox_test_sessions",
        sa.Column(
            "parent_session_id",
            sa.Integer(),
            sa.ForeignKey("sandbox_test_sessions.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "sandbox_test_sessions",
        sa.Column("rerun_scope", mysql.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sandbox_test_sessions", "rerun_scope")
    op.drop_column("sandbox_test_sessions", "parent_session_id")
    op.drop_column("sandbox_test_sessions", "step_statuses")
