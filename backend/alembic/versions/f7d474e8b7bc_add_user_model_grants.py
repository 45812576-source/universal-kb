"""add_user_model_grants

Revision ID: f7d474e8b7bc
Revises: p6q7r8s9t0u1
Create Date: 2026-03-19 15:27:34.845326

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f7d474e8b7bc'
down_revision: Union[str, Sequence[str], None] = 'p6q7r8s9t0u1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_model_grants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('model_key', sa.String(100), nullable=False),
        sa.Column('granted_by', sa.Integer(), nullable=True),
        sa.Column('granted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'model_key', name='uq_user_model_grant'),
    )


def downgrade() -> None:
    op.drop_table('user_model_grants')
