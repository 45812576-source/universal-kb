"""phase3: tool_registry, skill_tools, web_apps, intel_sources, intel_entries, users.lark_user_id

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tool_registry
    op.create_table(
        'tool_registry',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'tool_type',
            sa.Enum('mcp', 'builtin', 'http', name='tooltype'),
            nullable=False,
        ),
        sa.Column('config', mysql.JSON(), nullable=True),
        sa.Column('input_schema', mysql.JSON(), nullable=True),
        sa.Column('output_format', sa.String(length=50), server_default='json', nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    # 2. skill_tools (many-to-many)
    op.create_table(
        'skill_tools',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('tool_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.ForeignKeyConstraint(['tool_id'], ['tool_registry.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('skill_id', 'tool_id'),
    )

    # 3. web_apps
    op.create_table(
        'web_apps',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('html_content', sa.Text(length=16777215), nullable=True),  # MEDIUMTEXT
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('is_public', sa.Boolean(), server_default='0', nullable=True),
        sa.Column('share_token', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # 4. intel_sources
    op.create_table(
        'intel_sources',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column(
            'source_type',
            sa.Enum('rss', 'crawler', 'webhook', 'manual', name='intelsourcetype'),
            nullable=False,
        ),
        sa.Column('config', mysql.JSON(), nullable=True),
        sa.Column('schedule', sa.String(length=50), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # 5. intel_entries
    op.create_table(
        'intel_entries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('url', sa.String(length=1000), nullable=True),
        sa.Column('tags', mysql.JSON(), nullable=True),
        sa.Column('industry', sa.String(length=100), nullable=True),
        sa.Column('platform', sa.String(length=100), nullable=True),
        sa.Column(
            'status',
            sa.Enum('pending', 'approved', 'rejected', name='intelentrystatus'),
            nullable=True,
        ),
        sa.Column('auto_collected', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('vectorized', sa.Boolean(), server_default='0', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['intel_sources.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # 6. users.lark_user_id
    op.add_column('users', sa.Column('lark_user_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'lark_user_id')
    op.drop_table('intel_entries')
    op.drop_table('intel_sources')
    op.drop_table('web_apps')
    op.drop_table('skill_tools')
    op.drop_table('tool_registry')
