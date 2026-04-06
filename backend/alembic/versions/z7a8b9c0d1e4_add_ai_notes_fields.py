"""merge heads and add ai_notes fields to knowledge_entries

Revision ID: z7a8b9c0d1e4
Revises: 4fed96103626, 7680e768052d
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e4"
down_revision = ("4fed96103626", "7680e768052d")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_entries",
        sa.Column("ai_notes_html", sa.Text(length=4294967295), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("ai_notes_status", sa.String(20), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("ai_notes_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_entries", "ai_notes_error")
    op.drop_column("knowledge_entries", "ai_notes_status")
    op.drop_column("knowledge_entries", "ai_notes_html")
