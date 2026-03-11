"""add review level policy fields

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-03-08

Changes:
1. knowledge_entries: add review_level, review_stage, sensitivity_flags, auto_review_note
2. skills: add auto_save_output
3. Backfill: existing pending entries → review_level=2, review_stage='pending_dept'
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None


def upgrade():
    # 1. knowledge_entries: 新增分级审核字段
    op.add_column(
        'knowledge_entries',
        sa.Column('review_level', sa.Integer(), nullable=True, server_default='2'),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column(
            'review_stage',
            sa.Enum(
                'auto_approved',
                'pending_dept',
                'dept_approved_pending_super',
                'approved',
                'rejected',
                name='reviewstage',
            ),
            nullable=True,
            server_default='pending_dept',
        ),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('sensitivity_flags', JSON, nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('auto_review_note', sa.Text(), nullable=True),
    )

    # 2. skills: 新增 auto_save_output
    op.add_column(
        'skills',
        sa.Column('auto_save_output', sa.Boolean(), nullable=True, server_default='0'),
    )

    # 3. 回填现有数据
    # 已通过的知识条目 → review_stage=approved
    op.execute(
        "UPDATE knowledge_entries SET review_stage='approved' WHERE status='approved'"
    )
    # 已拒绝的知识条目 → review_stage=rejected
    op.execute(
        "UPDATE knowledge_entries SET review_stage='rejected' WHERE status='rejected'"
    )
    # PENDING 的条目 → review_level=2, review_stage=pending_dept
    op.execute(
        "UPDATE knowledge_entries "
        "SET review_level=2, review_stage='pending_dept' "
        "WHERE status='pending'"
    )


def downgrade():
    op.drop_column('skills', 'auto_save_output')
    op.drop_column('knowledge_entries', 'auto_review_note')
    op.drop_column('knowledge_entries', 'sensitivity_flags')
    op.drop_column('knowledge_entries', 'review_stage')
    op.drop_column('knowledge_entries', 'review_level')
    # drop enum type (MySQL handles enum inline, no separate type to drop)
