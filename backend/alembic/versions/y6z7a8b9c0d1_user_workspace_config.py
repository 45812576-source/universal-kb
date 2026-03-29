"""user_workspace_config 表 + workspace 新字段

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "y6z7a8b9c0d1"
down_revision = "x5y6z7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 新建 user_workspace_configs 表
    op.create_table(
        "user_workspace_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("mounted_skills", sa.JSON(), nullable=True),
        sa.Column("mounted_tools", sa.JSON(), nullable=True),
        sa.Column("skill_routing_prompt", sa.Text(), nullable=True),
        sa.Column("last_skill_snapshot", sa.JSON(), nullable=True),
        sa.Column("needs_prompt_refresh", sa.Boolean(), server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)")),
    )

    # workspace 表新增字段
    op.add_column("workspaces", sa.Column("is_preset", sa.Boolean(), server_default=sa.text("0")))
    op.add_column("workspaces", sa.Column("recommended_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("workspaces", sa.Column("for_department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True))

    # 标记系统内置工作台
    op.execute(
        "UPDATE workspaces SET is_preset = 1 "
        "WHERE workspace_type IN ('opencode', 'sandbox', 'skill_studio')"
    )


def downgrade() -> None:
    op.drop_column("workspaces", "for_department_id")
    op.drop_column("workspaces", "recommended_by")
    op.drop_column("workspaces", "is_preset")
    op.drop_table("user_workspace_configs")
