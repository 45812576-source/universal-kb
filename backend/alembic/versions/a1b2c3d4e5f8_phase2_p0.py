"""phase2 p0: business_tables, audit_logs, skill_data_queries, skills data_queries/tools

Revision ID: a1b2c3d4e5f8
Revises: 6bb20f9c928c
Create Date: 2026-03-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'a1b2c3d4e5f8'
down_revision: Union[str, Sequence[str], None] = '6bb20f9c928c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'data_ownership_rules',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('owner_field', sa.String(length=100), nullable=False),
        sa.Column('department_field', sa.String(length=100), nullable=True),
        sa.Column('visibility_level', sa.Enum('detail', 'desensitized', 'stats', name='visibilitylevel'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'business_tables',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('ddl_sql', sa.Text(), nullable=True),
        sa.Column('validation_rules', mysql.JSON(), nullable=True),
        sa.Column('workflow', mysql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('table_name'),
    )

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('operation', sa.String(length=20), nullable=False),
        sa.Column('row_id', sa.String(length=100), nullable=True),
        sa.Column('old_values', mysql.JSON(), nullable=True),
        sa.Column('new_values', mysql.JSON(), nullable=True),
        sa.Column('sql_executed', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'skill_data_queries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('query_name', sa.String(length=100), nullable=False),
        sa.Column('query_type', sa.String(length=20), nullable=False),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('template_sql', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.add_column('skills', sa.Column('data_queries', mysql.JSON(), nullable=True))
    op.add_column('skills', sa.Column('tools', mysql.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('skills', 'tools')
    op.drop_column('skills', 'data_queries')
    op.drop_table('skill_data_queries')
    op.drop_table('audit_logs')
    op.drop_table('business_tables')
    op.drop_table('data_ownership_rules')
