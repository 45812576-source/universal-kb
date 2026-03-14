"""dev project handoff fields

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-03-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "o5p6q7r8s9t0"
down_revision: Union[str, None] = "31697903fa71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # projects 表：新增 project_type 字段
    op.add_column(
        "projects",
        sa.Column("project_type", sa.String(20), nullable=False, server_default="custom"),
    )

    # project_contexts 表：新增交接相关字段
    op.add_column(
        "project_contexts",
        sa.Column("requirements", sa.Text(), nullable=True),
    )
    op.add_column(
        "project_contexts",
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
    )
    op.add_column(
        "project_contexts",
        sa.Column("handoff_status", sa.String(20), nullable=False, server_default="none"),
    )
    op.add_column(
        "project_contexts",
        sa.Column("handoff_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_contexts", "handoff_at")
    op.drop_column("project_contexts", "handoff_status")
    op.drop_column("project_contexts", "acceptance_criteria")
    op.drop_column("project_contexts", "requirements")
    op.drop_column("projects", "project_type")
