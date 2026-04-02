"""create knowledge_understanding_profiles table

Revision ID: b3c4d5e6f7g8
Revises: a1b2_cloud_doc
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "b3c4d5e6f7g8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_understanding_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("knowledge_id", sa.Integer(), nullable=False),
        # 自动命名
        sa.Column("display_title", sa.String(500), nullable=True),
        sa.Column("raw_title", sa.String(500), nullable=True),
        sa.Column("title_confidence", sa.Float(), nullable=True),
        sa.Column("title_source", sa.String(30), nullable=True),
        sa.Column("title_reason", sa.Text(), nullable=True),
        # 分类与权限标签
        sa.Column("document_type", sa.String(50), nullable=True),
        sa.Column("permission_domain", sa.String(50), nullable=True),
        sa.Column("desensitization_level", sa.String(20), nullable=True),
        sa.Column("contains_sensitive_data", sa.Boolean(), server_default="0"),
        sa.Column("data_type_hits", mysql.JSON(), nullable=True),
        sa.Column("visibility_recommendation", sa.String(30), nullable=True),
        # 5维内容标签
        sa.Column("content_tags", mysql.JSON(), nullable=True),
        sa.Column("suggested_tags", mysql.JSON(), nullable=True),
        # 摘要
        sa.Column("summary_short", sa.String(200), nullable=True),
        sa.Column("summary_search", sa.String(500), nullable=True),
        sa.Column("summary_sensitivity_mode", sa.String(20), nullable=True),
        # 来源追踪
        sa.Column("classification_source", sa.String(20), nullable=True),
        sa.Column("tagging_source", sa.String(20), nullable=True),
        sa.Column("masking_source", sa.String(20), nullable=True),
        sa.Column("summarization_source", sa.String(20), nullable=True),
        # 流水线状态
        sa.Column("understanding_status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("understanding_error", sa.Text(), nullable=True),
        sa.Column("understanding_version", sa.Integer(), server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["knowledge_id"], ["knowledge_entries.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_kup_knowledge_id",
        "knowledge_understanding_profiles",
        ["knowledge_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_kup_knowledge_id", table_name="knowledge_understanding_profiles")
    op.drop_table("knowledge_understanding_profiles")
