"""add user_saved_skills table

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-03-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_saved_skills',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('skill_id', sa.Integer, sa.ForeignKey('skills.id'), nullable=False),
        sa.Column('saved_at', sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'skill_id', name='uq_user_saved_skill'),
    )


def downgrade():
    op.drop_table('user_saved_skills')
