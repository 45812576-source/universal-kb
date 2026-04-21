"""org memory source bitable fields

Revision ID: g2h3i4j5k6l7
Revises: f1g2h3i4j5k6
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "g2h3i4j5k6l7"
down_revision = "f1g2h3i4j5k6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_memory_sources", sa.Column("bitable_app_token", sa.String(255), nullable=True))
    op.add_column("org_memory_sources", sa.Column("bitable_table_id", sa.String(255), nullable=True))
    op.add_column("org_memory_sources", sa.Column("raw_fields_json", JSON, nullable=True))
    op.add_column("org_memory_sources", sa.Column("raw_records_json", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("org_memory_sources", "raw_records_json")
    op.drop_column("org_memory_sources", "raw_fields_json")
    op.drop_column("org_memory_sources", "bitable_table_id")
    op.drop_column("org_memory_sources", "bitable_app_token")
