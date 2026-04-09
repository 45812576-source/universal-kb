"""Add docx_oss_key column for PDF-to-DOCX conversion

Revision ID: 4fed96103626
Revises: z7a8b9c0d1e3
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "4fed96103626"
down_revision = "z7a8b9c0d1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_entries",
        sa.Column("docx_oss_key", sa.String(500), nullable=True, comment="PDF 转换后的 DOCX 文件 OSS 路径"),
    )


def downgrade() -> None:
    op.drop_column("knowledge_entries", "docx_oss_key")
