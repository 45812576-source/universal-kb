"""master data and confirmations

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa

revision = 'g7h8i9j0k1l2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('skill_master',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('skill_code', sa.String(length=50), nullable=False),
        sa.Column('skill_name', sa.String(length=200), nullable=False),
        sa.Column('priority', sa.String(length=20), nullable=False),
        sa.Column('main_chain', sa.String(length=100), nullable=True),
        sa.Column('core_scenario', sa.Text(), nullable=True),
        sa.Column('primary_departments', sa.JSON(), nullable=True),
        sa.Column('primary_roles', sa.JSON(), nullable=True),
        sa.Column('low_friction_input', sa.JSON(), nullable=True),
        sa.Column('system_inputs', sa.JSON(), nullable=True),
        sa.Column('system_outputs', sa.JSON(), nullable=True),
        sa.Column('artifact_type', sa.String(length=100), nullable=True),
        sa.Column('knowledge_layers', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('skill_code')
    )

    op.create_table('input_taxonomy',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('taxonomy_code', sa.String(length=50), nullable=False),
        sa.Column('level_1_business_object', sa.String(length=100), nullable=False),
        sa.Column('level_2_evidence_purpose', sa.String(length=100), nullable=False),
        sa.Column('level_3_storage_form', sa.String(length=50), nullable=False),
        sa.Column('level_4_system_stage', sa.String(length=50), nullable=False),
        sa.Column('category_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('typical_examples', sa.JSON(), nullable=True),
        sa.Column('supported_input_actions', sa.JSON(), nullable=True),
        sa.Column('target_objects', sa.JSON(), nullable=True),
        sa.Column('default_artifact_types', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('taxonomy_code')
    )

    op.create_table('object_field_dictionary',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('field_group', sa.String(length=100), nullable=False),
        sa.Column('object_type', sa.String(length=100), nullable=False),
        sa.Column('field_name', sa.String(length=100), nullable=False),
        sa.Column('field_label', sa.String(length=200), nullable=False),
        sa.Column('field_type', sa.String(length=50), nullable=False),
        sa.Column('field_description', sa.Text(), nullable=True),
        sa.Column('source_layer', sa.String(length=100), nullable=True),
        sa.Column('source_method', sa.String(length=200), nullable=True),
        sa.Column('confirmation_mode', sa.String(length=50), nullable=True),
        sa.Column('storage_layer', sa.String(length=50), nullable=True),
        sa.Column('example_values', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_type', 'field_name', name='uq_object_field_dictionary_object_field')
    )

    op.create_table('confirmations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('draft_id', sa.Integer(), nullable=False),
        sa.Column('field_name', sa.String(length=100), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('question_type', sa.String(length=50), server_default='single_choice', nullable=False),
        sa.Column('options_json', sa.JSON(), nullable=True),
        sa.Column('suggested_value', sa.Text(), nullable=True),
        sa.Column('confirmed_value', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('confidence', sa.String(length=20), nullable=True),
        sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['confirmed_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['draft_id'], ['drafts.id']),
        sa.PrimaryKeyConstraint('id')
    )

    op.add_column('drafts', sa.Column('formal_object_type', sa.String(length=100), nullable=True))
    op.add_column('learning_samples', sa.Column('confidence', sa.Numeric(5, 4), nullable=True))
    op.add_column('learning_samples', sa.Column('reviewed_by_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_learning_samples_reviewed_by_id_users', 'learning_samples', 'users', ['reviewed_by_id'], ['id'])

    op.add_column('knowledge_entries', sa.Column('visibility_scope', sa.String(length=50), nullable=True))
    op.add_column('knowledge_entries', sa.Column('linked_skill_codes', sa.JSON(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('applicable_departments', sa.JSON(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('applicable_roles', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('knowledge_entries', 'applicable_roles')
    op.drop_column('knowledge_entries', 'applicable_departments')
    op.drop_column('knowledge_entries', 'linked_skill_codes')
    op.drop_column('knowledge_entries', 'visibility_scope')

    op.drop_constraint('fk_learning_samples_reviewed_by_id_users', 'learning_samples', type_='foreignkey')
    op.drop_column('learning_samples', 'reviewed_by_id')
    op.drop_column('learning_samples', 'confidence')
    op.drop_column('drafts', 'formal_object_type')

    op.drop_table('confirmations')
    op.drop_table('object_field_dictionary')
    op.drop_table('input_taxonomy')
    op.drop_table('skill_master')
