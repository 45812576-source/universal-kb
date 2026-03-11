"""add knowledge taxonomy classification fields

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-03-08

Changes:
1. knowledge_entries: add taxonomy_code, taxonomy_board, taxonomy_path,
   storage_layer, target_kb_ids, serving_skill_codes,
   ai_classification_note, classification_confidence
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'knowledge_entries',
        sa.Column('taxonomy_code', sa.String(50), nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('taxonomy_board', sa.String(10), nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('taxonomy_path', JSON, nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('storage_layer', sa.String(10), nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('target_kb_ids', JSON, nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('serving_skill_codes', JSON, nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('ai_classification_note', sa.Text(), nullable=True),
    )
    op.add_column(
        'knowledge_entries',
        sa.Column('classification_confidence', sa.Float(), nullable=True),
    )


def downgrade():
    op.drop_column('knowledge_entries', 'classification_confidence')
    op.drop_column('knowledge_entries', 'ai_classification_note')
    op.drop_column('knowledge_entries', 'serving_skill_codes')
    op.drop_column('knowledge_entries', 'target_kb_ids')
    op.drop_column('knowledge_entries', 'storage_layer')
    op.drop_column('knowledge_entries', 'taxonomy_path')
    op.drop_column('knowledge_entries', 'taxonomy_board')
    op.drop_column('knowledge_entries', 'taxonomy_code')
