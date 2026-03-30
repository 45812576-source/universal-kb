"""add cloud doc render and sync fields to knowledge_entries

Revision ID: a1b2_cloud_doc
Revises: z7a8b9c0d1e2
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2_cloud_doc"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 云文档渲染状态
    op.add_column(
        "knowledge_entries",
        sa.Column("doc_render_status", sa.String(20), nullable=True, server_default="pending"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("doc_render_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("doc_render_mode", sa.String(30), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("last_rendered_at", sa.DateTime(), nullable=True),
    )
    # 统一来源 URI
    op.add_column(
        "knowledge_entries",
        sa.Column("source_uri", sa.String(500), nullable=True),
    )
    # 飞书同步状态
    op.add_column(
        "knowledge_entries",
        sa.Column("sync_status", sa.String(20), nullable=True, server_default="idle"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("sync_error", sa.Text(), nullable=True),
    )

    # 把已有的 content_html 非空条目标记为 ready
    op.execute(
        "UPDATE knowledge_entries SET doc_render_status = 'ready', "
        "doc_render_mode = 'native_html' "
        "WHERE content_html IS NOT NULL AND content_html != ''"
    )
    # 飞书来源的条目设置 sync_status = ok
    op.execute(
        "UPDATE knowledge_entries SET sync_status = 'ok' "
        "WHERE source_type = 'lark_doc'"
    )


def downgrade() -> None:
    op.drop_column("knowledge_entries", "sync_error")
    op.drop_column("knowledge_entries", "sync_status")
    op.drop_column("knowledge_entries", "source_uri")
    op.drop_column("knowledge_entries", "last_rendered_at")
    op.drop_column("knowledge_entries", "doc_render_mode")
    op.drop_column("knowledge_entries", "doc_render_error")
    op.drop_column("knowledge_entries", "doc_render_status")
