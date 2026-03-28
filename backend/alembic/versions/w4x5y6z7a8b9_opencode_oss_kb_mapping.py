"""opencode workdir OSS 迁移 + 知识库"开发工地"文件夹映射

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade():
    # 1. opencode_workspace_mappings 新增 OSS 前缀字段
    op.add_column(
        "opencode_workspace_mappings",
        sa.Column("oss_prefix", sa.String(500), nullable=True, comment="OSS 路径前缀，如 studio_workspaces/胡瑞"),
    )
    # 2. 新增知识库"开发工地"文件夹 ID 映射
    op.add_column(
        "opencode_workspace_mappings",
        sa.Column("kb_folder_id", sa.Integer(), nullable=True, comment="对应知识库'开发工地'文件夹 ID"),
    )
    # 3. 修正历史数据中的 /tmp 路径（标记为无效，让应用重建）
    op.execute(
        "UPDATE opencode_workspace_mappings SET directory = NULL WHERE directory LIKE '/tmp/%'"
    )
    # 4. 修正路径前缀：统一使用 /home/mo/codes/project/studio_workspaces（与 .env 一致）
    op.execute(
        "UPDATE opencode_workspace_mappings "
        "SET directory = REPLACE(directory, '/home/mo/studio_workspaces/', '/home/mo/codes/project/studio_workspaces/') "
        "WHERE directory LIKE '/home/mo/studio_workspaces/%'"
    )


def downgrade():
    op.drop_column("opencode_workspace_mappings", "kb_folder_id")
    op.drop_column("opencode_workspace_mappings", "oss_prefix")
