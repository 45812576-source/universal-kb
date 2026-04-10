"""add assigned_approver_id to approval_requests

Revision ID: e1dcf23f4bbc
Revises: 647f0db9f80d
Create Date: 2026-04-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1dcf23f4bbc'
down_revision: Union[str, Sequence[str], None] = '647f0db9f80d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('approval_requests', sa.Column('assigned_approver_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_approval_requests_assigned_approver',
        'approval_requests', 'users',
        ['assigned_approver_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_approval_requests_assigned_approver', 'approval_requests', type_='foreignkey')
    op.drop_column('approval_requests', 'assigned_approver_id')
