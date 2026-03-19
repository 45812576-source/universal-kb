"""add_approval_stage

Revision ID: g8h9i0j1k2l3
Revises: f7d474e8b7bc
Create Date: 2026-03-19 16:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'g8h9i0j1k2l3'
down_revision: Union[str, Sequence[str], None] = 'f7d474e8b7bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'approval_requests',
        sa.Column('stage', sa.String(20), nullable=False, server_default='dept_pending'),
    )


def downgrade() -> None:
    op.drop_column('approval_requests', 'stage')
