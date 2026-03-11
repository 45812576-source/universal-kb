"""add tasks table

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-08

Changes:
1. Create tasks table with Eisenhower matrix priority, status, assignee, source tracking
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'priority',
            sa.Enum('urgent_important', 'important', 'urgent', 'neither', name='taskpriority'),
            nullable=True,
            server_default='neither',
        ),
        sa.Column(
            'status',
            sa.Enum('pending', 'in_progress', 'done', 'cancelled', name='taskstatus'),
            nullable=True,
            server_default='pending',
        ),
        sa.Column('due_date', sa.DateTime(), nullable=True),
        sa.Column('assignee_id', sa.Integer(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(50), nullable=True, server_default='manual'),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('metadata', JSON, nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['assignee_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tasks_assignee_id', 'tasks', ['assignee_id'])
    op.create_index('ix_tasks_created_by_id', 'tasks', ['created_by_id'])
    op.create_index('ix_tasks_status', 'tasks', ['status'])


def downgrade():
    op.drop_index('ix_tasks_status', 'tasks')
    op.drop_index('ix_tasks_created_by_id', 'tasks')
    op.drop_index('ix_tasks_assignee_id', 'tasks')
    op.drop_table('tasks')
