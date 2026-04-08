"""add missing columns to knowledge_understanding_profiles

Revision ID: z7a8b9c0d1e6
Revises: z7a8b9c0d1e5
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e6"
down_revision = "z7a8b9c0d1e5"
branch_labels = None
depends_on = None

_TABLE = "knowledge_understanding_profiles"

_COLUMNS = [
    ("content_tag_confidences", sa.JSON, {}),
    ("summary_embedding", sa.String(500), {}),
    ("system_id", sa.String(30), {}),
    ("confirmed_at", sa.DateTime, {}),
    ("confirmed_by", sa.Integer, {}),
    ("user_corrections", sa.JSON, {}),
]


def _col_exists(conn, col_name: str) -> bool:
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_name='{_TABLE}' AND column_name=:col"
    ), {"col": col_name})
    return result.scalar() > 0


def upgrade() -> None:
    conn = op.get_bind()
    for col_name, col_type, kwargs in _COLUMNS:
        if not _col_exists(conn, col_name):
            op.add_column(_TABLE, sa.Column(col_name, col_type, nullable=True, **kwargs))

    # system_id 需要唯一索引
    if not _col_exists(conn, "system_id"):
        pass  # 刚加的列，下面加索引
    try:
        op.create_index(f"ix_{_TABLE}_system_id", _TABLE, ["system_id"], unique=True)
    except Exception:
        pass  # 索引可能已存在


def downgrade() -> None:
    for col_name, _, _ in reversed(_COLUMNS):
        try:
            op.drop_column(_TABLE, col_name)
        except Exception:
            pass
