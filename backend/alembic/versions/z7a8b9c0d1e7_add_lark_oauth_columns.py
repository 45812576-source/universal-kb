"""add lark oauth token columns to users

Revision ID: z7a8b9c0d1e7
Revises: z7a8b9c0d1e6
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e7"
down_revision = "z7a8b9c0d1e6"
branch_labels = None
depends_on = None

_TABLE = "users"

_COLUMNS = [
    ("lark_access_token", sa.Text),
    ("lark_refresh_token", sa.Text),
    ("lark_token_expires_at", sa.DateTime),
]


def _col_exists(conn, col_name: str) -> bool:
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_name='{_TABLE}' AND column_name=:col"
    ), {"col": col_name})
    return result.scalar() > 0


def upgrade() -> None:
    conn = op.get_bind()
    for col_name, col_type in _COLUMNS:
        if not _col_exists(conn, col_name):
            op.add_column(_TABLE, sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    for col_name, _ in reversed(_COLUMNS):
        try:
            op.drop_column(_TABLE, col_name)
        except Exception:
            pass
