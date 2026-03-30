"""phase2 p1+p2: data_ownership_rules, skill_suggestions, skill_attributions

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # P1: data_ownership_rules
    op.create_table(
        'data_ownership_rules',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('owner_field', sa.String(length=100), nullable=False),
        sa.Column('department_field', sa.String(length=100), nullable=True),
        sa.Column(
            'visibility_level',
            sa.Enum('detail', 'desensitized', 'stats', name='visibilitylevel'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # P2: skill_suggestions
    op.create_table(
        'skill_suggestions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('submitted_by', sa.Integer(), nullable=False),
        sa.Column('problem_desc', sa.Text(), nullable=False),
        sa.Column('expected_direction', sa.Text(), nullable=False),
        sa.Column('case_example', sa.Text(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('pending', 'adopted', 'partial', 'rejected', name='suggestionstatus'),
            nullable=True,
        ),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # P2: skill_attributions
    op.create_table(
        'skill_attributions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('version_from', sa.Integer(), nullable=False),
        sa.Column('version_to', sa.Integer(), nullable=False),
        sa.Column('suggestion_id', sa.Integer(), nullable=False),
        sa.Column(
            'attribution_level',
            sa.Enum('full', 'partial', 'none', name='attributionlevel'),
            nullable=False,
        ),
        sa.Column('matched_change', sa.Text(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.ForeignKeyConstraint(['suggestion_id'], ['skill_suggestions.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('skill_attributions')
    op.drop_table('skill_suggestions')
    op.drop_table('data_ownership_rules')
