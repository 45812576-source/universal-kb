"""workspaces: workspace + binding tables + conversations.workspace_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-07 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. workspaces
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('icon', sa.String(length=50), server_default='chat', nullable=True),
        sa.Column('color', sa.String(length=20), server_default='#00D1FF', nullable=True),
        sa.Column('category', sa.String(length=50), server_default='通用', nullable=True),
        sa.Column(
            'status',
            sa.Enum('draft', 'reviewing', 'published', 'archived', name='workspacestatus'),
            server_default='draft',
            nullable=False,
        ),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('visibility', sa.String(length=20), server_default='all', nullable=True),
        sa.Column('welcome_message', sa.Text(), nullable=True),
        sa.Column('system_context', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # 2. workspace_skills
    op.create_table(
        'workspace_skills',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'skill_id'),
    )

    # 3. workspace_tools
    op.create_table(
        'workspace_tools',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('tool_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['tool_id'], ['tool_registry.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'tool_id'),
    )

    # 4. workspace_data_tables
    op.create_table(
        'workspace_data_tables',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('table_name', sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # 5. conversations.workspace_id
    op.add_column('conversations', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_conversations_workspace_id',
        'conversations', 'workspaces',
        ['workspace_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_conversations_workspace_id', 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'workspace_id')
    op.drop_table('workspace_data_tables')
    op.drop_table('workspace_tools')
    op.drop_table('workspace_skills')
    op.drop_table('workspaces')
