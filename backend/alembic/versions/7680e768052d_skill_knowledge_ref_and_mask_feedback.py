"""skill_knowledge_ref_and_mask_feedback

Revision ID: 7680e768052d
Revises: 958f5379c02e
Create Date: 2026-04-03 11:46:37.134907

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '7680e768052d'
down_revision: Union[str, Sequence[str], None] = '958f5379c02e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 新表: skill_knowledge_references ──────────────────────────────────
    op.create_table(
        'skill_knowledge_references',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('knowledge_id', sa.Integer(), nullable=False),
        sa.Column('snapshot_desensitization_level', sa.String(20), nullable=True),
        sa.Column('snapshot_data_type_hits', mysql.JSON(), nullable=True),
        sa.Column('snapshot_document_type', sa.String(50), nullable=True),
        sa.Column('snapshot_permission_domain', sa.String(50), nullable=True),
        sa.Column('snapshot_mask_rules', mysql.JSON(), nullable=True),
        sa.Column('mask_rule_source', sa.String(30), nullable=True),
        sa.Column('folder_id', sa.Integer(), nullable=True),
        sa.Column('folder_path', sa.String(500), nullable=True),
        sa.Column('manager_scope_ok', sa.Boolean(), nullable=True),
        sa.Column('publish_version', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge_entries.id']),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('skill_id', 'knowledge_id', 'publish_version', name='uq_skill_knowledge_ref'),
    )
    op.create_index(op.f('ix_skill_knowledge_references_skill_id'), 'skill_knowledge_references', ['skill_id'])

    # ── 新表: knowledge_mask_feedbacks ────────────────────────────────────
    op.create_table(
        'knowledge_mask_feedbacks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('knowledge_id', sa.Integer(), nullable=False),
        sa.Column('understanding_profile_id', sa.Integer(), nullable=True),
        sa.Column('submitted_by', sa.Integer(), nullable=False),
        sa.Column('current_desensitization_level', sa.String(20), nullable=True),
        sa.Column('current_data_type_hits', mysql.JSON(), nullable=True),
        sa.Column('suggested_desensitization_level', sa.String(20), nullable=True),
        sa.Column('suggested_data_type_adjustments', mysql.JSON(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('evidence_snippet', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('review_action', sa.String(30), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge_entries.id']),
        sa.ForeignKeyConstraint(['understanding_profile_id'], ['knowledge_understanding_profiles.id']),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_knowledge_mask_feedbacks_knowledge_id'), 'knowledge_mask_feedbacks', ['knowledge_id'])

    # ── 新表: knowledge_mask_rule_versions ────────────────────────────────
    op.create_table(
        'knowledge_mask_rule_versions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('changes', mysql.JSON(), nullable=True),
        sa.Column('approved_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version'),
    )

    # ── 扩展: knowledge_understanding_profiles ───────────────────────────
    op.add_column('knowledge_understanding_profiles', sa.Column('mask_rule_version', sa.Integer(), nullable=True))
    op.add_column('knowledge_understanding_profiles', sa.Column('correction_status', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('knowledge_understanding_profiles', 'correction_status')
    op.drop_column('knowledge_understanding_profiles', 'mask_rule_version')
    op.drop_table('knowledge_mask_rule_versions')
    op.drop_index(op.f('ix_knowledge_mask_feedbacks_knowledge_id'), table_name='knowledge_mask_feedbacks')
    op.drop_table('knowledge_mask_feedbacks')
    op.drop_index(op.f('ix_skill_knowledge_references_skill_id'), table_name='skill_knowledge_references')
    op.drop_table('skill_knowledge_references')
