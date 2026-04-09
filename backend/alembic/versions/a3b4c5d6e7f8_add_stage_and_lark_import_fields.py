"""add stage to table_sync_jobs, ensure knowledge_jobs columns exist

Revision ID: a3b4c5d6e7f8
Revises: z7a8b9c0d2a2
Create Date: 2026-04-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "a3b4c5d6e7f8"
down_revision = "z7a8b9c0d2a2"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    """检查列是否已存在（MySQL）。"""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.scalar() > 0


def _table_exists(table: str) -> bool:
    """检查表是否已存在（MySQL）。"""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :t"
    ), {"t": table})
    return result.scalar() > 0


def upgrade() -> None:
    # 1. table_sync_jobs.stage — 新增阶段字段
    if not _column_exists("table_sync_jobs", "stage"):
        op.add_column(
            "table_sync_jobs",
            sa.Column("stage", sa.String(30), nullable=True),
        )

    # 2. knowledge_jobs 表可能由运行时 create_all 创建，但缺少迁移。
    #    确保表存在且关键列齐全。
    if not _table_exists("knowledge_jobs"):
        op.create_table(
            "knowledge_jobs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("knowledge_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id"), nullable=True),
            sa.Column("subject_type", sa.String(30), server_default="knowledge", nullable=False),
            sa.Column("subject_id", sa.Integer(), nullable=True),
            sa.Column("job_type", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), server_default="queued", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0"),
            sa.Column("max_attempts", sa.Integer(), server_default="3"),
            sa.Column("error_type", sa.String(50), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("phase", sa.String(30), nullable=True),
            sa.Column("trigger_source", sa.String(20), server_default="upload"),
            sa.Column("payload", JSON, nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_jobs_knowledge_id", "knowledge_jobs", ["knowledge_id"])
        op.create_index("ix_knowledge_jobs_subject_type", "knowledge_jobs", ["subject_type"])
        op.create_index("ix_knowledge_jobs_status", "knowledge_jobs", ["status"])
        op.create_index("ix_knowledge_jobs_job_type", "knowledge_jobs", ["job_type"])
    else:
        # 表已存在，补缺失列
        if not _column_exists("knowledge_jobs", "phase"):
            op.add_column(
                "knowledge_jobs",
                sa.Column("phase", sa.String(30), nullable=True),
            )
        if not _column_exists("knowledge_jobs", "created_by"):
            op.add_column(
                "knowledge_jobs",
                sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            )
        if not _column_exists("knowledge_jobs", "subject_type"):
            op.add_column(
                "knowledge_jobs",
                sa.Column("subject_type", sa.String(30), server_default="knowledge", nullable=False),
            )
        if not _column_exists("knowledge_jobs", "subject_id"):
            op.add_column(
                "knowledge_jobs",
                sa.Column("subject_id", sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    if _column_exists("table_sync_jobs", "stage"):
        op.drop_column("table_sync_jobs", "stage")
    if _column_exists("knowledge_jobs", "created_by"):
        op.drop_column("knowledge_jobs", "created_by")
