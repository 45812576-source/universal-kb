"""add content_html to knowledge_entries

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_entries",
        sa.Column("content_html", sa.Text(length=4294967295), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("knowledge_entries", "content_html")
