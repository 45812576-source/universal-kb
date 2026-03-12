"""add pev tables

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-03-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers
revision: str = "n4o5p6q7r8s9"
down_revision: Union[str, None] = "60d57209f955"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pev_jobs 表
    op.create_table(
        "pev_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "status",
            sa.Enum(
                "planning", "executing", "verifying", "completed", "failed", "cancelled",
                name="pevjobstatus",
            ),
            nullable=False,
            server_default="planning",
        ),
        sa.Column("scenario", sa.String(50), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("plan", mysql.JSON(), nullable=True),
        sa.Column("context", mysql.JSON(), nullable=True),
        sa.Column("config", mysql.JSON(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("intel_task_id", sa.Integer(), sa.ForeignKey("intel_tasks.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("total_steps", sa.Integer(), default=0),
        sa.Column("completed_steps", sa.Integer(), default=0),
        sa.Column("current_step_index", sa.Integer(), default=0),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # pev_steps 表
    op.create_table(
        "pev_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("pev_jobs.id"), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, default=0),
        sa.Column("step_key", sa.String(100), nullable=False),
        sa.Column("step_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("depends_on", mysql.JSON(), nullable=True),
        sa.Column("input_spec", mysql.JSON(), nullable=True),
        sa.Column("output_spec", mysql.JSON(), nullable=True),
        sa.Column("verify_criteria", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "passed", "failed", "skipped", name="pevstepstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("result", mysql.JSON(), nullable=True),
        sa.Column("verify_result", mysql.JSON(), nullable=True),
        sa.Column("retry_count", sa.Integer(), default=0),
    )

    # tasks 表添加 pev_job_id 列
    op.add_column(
        "tasks",
        sa.Column("pev_job_id", sa.Integer(), sa.ForeignKey("pev_jobs.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "pev_job_id")
    op.drop_table("pev_steps")
    op.drop_table("pev_jobs")
