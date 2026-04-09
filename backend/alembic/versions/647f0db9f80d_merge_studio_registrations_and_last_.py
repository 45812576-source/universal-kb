"""merge studio_registrations and last_verified_at branches

Revision ID: 647f0db9f80d
Revises: a3b4c5d6e7f8, b2c3d4e5f6a8
Create Date: 2026-04-09 23:54:34.442445

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '647f0db9f80d'
down_revision: Union[str, Sequence[str], None] = ('a3b4c5d6e7f8', 'b2c3d4e5f6a8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
