"""审批类型扩展: knowledge_review, skill_version_change, skill_ownership_transfer

Revision ID: z7a8b9c0d1e3
Revises: y6z7a8b9c0d1
Create Date: 2026-03-30
"""
from alembic import op

revision = "z7a8b9c0d1e3"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL: ALTER 枚举类型增加新值
    op.execute(
        "ALTER TABLE approval_requests MODIFY COLUMN request_type "
        "ENUM('skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review') NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE approval_requests MODIFY COLUMN request_type "
        "ENUM('skill_publish','tool_publish','webapp_publish','scope_change',"
        "'mask_override','schema_approval','knowledge_edit') NOT NULL"
    )
