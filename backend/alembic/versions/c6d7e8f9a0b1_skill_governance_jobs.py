"""skill governance async jobs

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_governance_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(64), nullable=True),
        sa.Column("payload_json", mysql.JSON(), nullable=True),
        sa.Column("result_json", mysql.JSON(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sgj_skill_status_created", "skill_governance_jobs", ["skill_id", "status", "created_at"])
    op.create_index("idx_sgj_type_status", "skill_governance_jobs", ["job_type", "status"])


def downgrade() -> None:
    op.drop_index("idx_sgj_type_status", table_name="skill_governance_jobs")
    op.drop_index("idx_sgj_skill_status_created", table_name="skill_governance_jobs")
    op.drop_table("skill_governance_jobs")
