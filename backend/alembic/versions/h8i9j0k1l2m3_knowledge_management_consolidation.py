"""knowledge management consolidation

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-03-08

Changes:
1. New table: knowledge_revisions
2. skills table: add scope column
3. skill_suggestions table: add source_message_id, reaction_type columns
4. intel_sources table: add managed_by, authorized_user_ids columns
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    # 1. knowledge_revisions table
    op.create_table(
        'knowledge_revisions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('knowledge_id', sa.Integer(), sa.ForeignKey('knowledge_entries.id'), nullable=False),
        sa.Column('user_request', sa.Text(), nullable=True),
        sa.Column('diff_content', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('visibility', sa.String(50), nullable=True, server_default='super_admin_only'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 2. skills.scope
    op.add_column('skills', sa.Column('scope', sa.String(20), nullable=True, server_default='personal'))

    # 3. skill_suggestions: source_message_id, reaction_type
    op.add_column('skill_suggestions', sa.Column(
        'source_message_id', sa.Integer(),
        sa.ForeignKey('messages.id', ondelete='SET NULL'),
        nullable=True,
    ))
    op.add_column('skill_suggestions', sa.Column('reaction_type', sa.String(20), nullable=True))

    # 4. intel_sources: managed_by, authorized_user_ids
    op.add_column('intel_sources', sa.Column(
        'managed_by', sa.Integer(),
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    ))
    op.add_column('intel_sources', sa.Column('authorized_user_ids', JSON, nullable=True))


def downgrade():
    op.drop_column('intel_sources', 'authorized_user_ids')
    op.drop_column('intel_sources', 'managed_by')
    op.drop_column('skill_suggestions', 'reaction_type')
    op.drop_column('skill_suggestions', 'source_message_id')
    op.drop_column('skills', 'scope')
    op.drop_table('knowledge_revisions')
