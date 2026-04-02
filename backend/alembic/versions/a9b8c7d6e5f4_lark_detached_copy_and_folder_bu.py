"""add folder business_unit and lark detached copy mode

Revision ID: a9b8c7d6e5f4
Revises: z7a8b9c0d1e3_approval_type_expansion
Create Date: 2026-04-02 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "z7a8b9c0d1e3_approval_type_expansion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("knowledge_folders", "business_unit"):
        op.add_column("knowledge_folders", sa.Column("business_unit", sa.String(length=100), nullable=True))

    if not _has_column("knowledge_entries", "external_edit_mode"):
        op.add_column("knowledge_entries", sa.Column("external_edit_mode", sa.String(length=50), nullable=True))


def downgrade() -> None:
    if _has_column("knowledge_entries", "external_edit_mode"):
        op.drop_column("knowledge_entries", "external_edit_mode")

    if _has_column("knowledge_folders", "business_unit"):
        op.drop_column("knowledge_folders", "business_unit")
