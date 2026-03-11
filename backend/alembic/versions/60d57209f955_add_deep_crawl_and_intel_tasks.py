"""add_deep_crawl_and_intel_tasks

Revision ID: 60d57209f955
Revises: m3n4o5p6q7r8
Create Date: 2026-03-11 09:22:07.114947

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '60d57209f955'
down_revision: Union[str, Sequence[str], None] = 'm3n4o5p6q7r8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. 创建 intel_tasks 表
    op.create_table('intel_tasks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.Enum('queued', 'running', 'completed', 'failed', name='inteltaskstatus'), nullable=True),
        sa.Column('total_urls', sa.Integer(), nullable=True),
        sa.Column('crawled_urls', sa.Integer(), nullable=True),
        sa.Column('new_entries', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['intel_sources.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    # 2. intel_entries 新增字段
    op.add_column('intel_entries', sa.Column('raw_markdown', sa.Text(), nullable=True))
    op.add_column('intel_entries', sa.Column('depth', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('intel_entries', 'depth')
    op.drop_column('intel_entries', 'raw_markdown')
    op.drop_table('intel_tasks')
