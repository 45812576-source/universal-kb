"""add knowledge_folders table and folder_id to knowledge_entries

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-03-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'knowledge_folders',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('parent_id', sa.Integer, sa.ForeignKey('knowledge_folders.id'), nullable=True),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('department_id', sa.Integer, sa.ForeignKey('departments.id'), nullable=True),
        sa.Column('sort_order', sa.Integer, default=0),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('folder_id', sa.Integer, sa.ForeignKey('knowledge_folders.id'), nullable=True),
    )


def downgrade():
    op.drop_column('knowledge_entries', 'folder_id')
    op.drop_table('knowledge_folders')
