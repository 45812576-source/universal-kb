"""add governance suggestion audit columns

Revision ID: d6e7f8g9h0i1
Revises: c6d7e8f9a0b1
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa


revision = "d6e7f8g9h0i1"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("governance_suggestion_tasks", "auto_applied"):
        op.add_column(
            "governance_suggestion_tasks",
            sa.Column("auto_applied", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        )
    if not _has_column("governance_suggestion_tasks", "candidates_payload"):
        op.add_column(
            "governance_suggestion_tasks",
            sa.Column("candidates_payload", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("governance_suggestion_tasks", "candidates_payload"):
        op.drop_column("governance_suggestion_tasks", "candidates_payload")
    if _has_column("governance_suggestion_tasks", "auto_applied"):
        op.drop_column("governance_suggestion_tasks", "auto_applied")
