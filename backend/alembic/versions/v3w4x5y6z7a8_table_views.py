"""add table_views for per-table saved filter/sort/group views

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'v3w4x5y6z7a8'
down_revision = 'u2v3w4x5y6z7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'table_views',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('table_id', sa.Integer(), sa.ForeignKey('business_tables.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('view_type', sa.String(20), nullable=True, server_default='grid'),
        sa.Column('config', mysql.JSON(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_table_views_table_id', 'table_views', ['table_id'])


def downgrade():
    op.drop_index('ix_table_views_table_id', 'table_views')
    op.drop_table('table_views')
