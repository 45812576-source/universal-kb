"""opencode_usage_cache 增加 ai_calls 字段

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'x5y6z7a8b9c0'
down_revision = 'w4x5y6z7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'opencode_usage_cache',
        sa.Column('ai_calls', sa.Integer(), nullable=True, server_default='0'),
    )


def downgrade():
    op.drop_column('opencode_usage_cache', 'ai_calls')
