"""skill_market_mcp: upstream tracking + mcp_sources + mcp_tokens + skill_upstream_checks

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-07 21:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Extend skills table with upstream tracking columns
    op.add_column('skills', sa.Column('source_type', sa.String(20), server_default='local', nullable=True))
    op.add_column('skills', sa.Column('upstream_url', sa.String(500), nullable=True))
    op.add_column('skills', sa.Column('upstream_id', sa.String(200), nullable=True))
    op.add_column('skills', sa.Column('upstream_version', sa.String(50), nullable=True))
    op.add_column('skills', sa.Column('upstream_content', sa.Text(), nullable=True))
    op.add_column('skills', sa.Column('upstream_synced_at', sa.DateTime(), nullable=True))
    op.add_column('skills', sa.Column('is_customized', sa.Boolean(), server_default='0', nullable=True))
    op.add_column('skills', sa.Column('parent_skill_id', sa.Integer(), nullable=True))
    op.add_column('skills', sa.Column('local_modified_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('fk_skills_parent_skill_id', 'skills', 'skills', ['parent_skill_id'], ['id'])

    # 2. mcp_sources
    op.create_table(
        'mcp_sources',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('url', sa.String(500), nullable=False),
        sa.Column('adapter_type', sa.String(20), server_default='mcp', nullable=True),
        sa.Column('auth_token', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # 3. mcp_tokens
    op.create_table(
        'mcp_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('token_hash', sa.String(200), nullable=False),
        sa.Column('token_prefix', sa.String(12), nullable=False),
        sa.Column('scope', sa.Enum('user', 'workspace', 'admin', name='mcptokenscope'), server_default='user', nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )

    # 4. skill_upstream_checks
    op.create_table(
        'skill_upstream_checks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('upstream_version', sa.String(50), nullable=True),
        sa.Column('has_diff', sa.Boolean(), server_default='0', nullable=True),
        sa.Column('diff_summary', sa.Text(), nullable=True),
        sa.Column('action', sa.String(20), server_default='pending', nullable=True),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('skill_upstream_checks')
    op.drop_table('mcp_tokens')
    op.drop_table('mcp_sources')
    op.drop_constraint('fk_skills_parent_skill_id', 'skills', type_='foreignkey')
    for col in ['local_modified_at', 'parent_skill_id', 'is_customized',
                'upstream_synced_at', 'upstream_content', 'upstream_version',
                'upstream_id', 'upstream_url', 'source_type']:
        op.drop_column('skills', col)
