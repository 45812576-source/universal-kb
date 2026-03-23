"""add source_files json column to skills

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 's9t0u1v2w3x4'
down_revision = 'r8s9t0u1v2w3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('skills', sa.Column('source_files', mysql.JSON(), nullable=True))


def downgrade():
    op.drop_column('skills', 'source_files')
