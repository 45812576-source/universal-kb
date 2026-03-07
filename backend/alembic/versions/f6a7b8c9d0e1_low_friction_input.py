"""low friction input tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('raw_inputs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(50), nullable=False, server_default='text'),
        sa.Column('source_channel', sa.String(50), server_default='web'),
        sa.Column('raw_text', sa.Text(), nullable=True),
        sa.Column('attachment_urls', sa.JSON(), nullable=True),
        sa.Column('context_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(50), server_default='received'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('input_extractions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('raw_input_id', sa.Integer(), nullable=False),
        sa.Column('detected_intent', sa.String(200), nullable=True),
        sa.Column('detected_object_type', sa.String(50), server_default='unknown'),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('entities_json', sa.JSON(), nullable=True),
        sa.Column('fields_json', sa.JSON(), nullable=True),
        sa.Column('confidence_json', sa.JSON(), nullable=True),
        sa.Column('uncertain_fields', sa.JSON(), nullable=True),
        sa.Column('extractor_version', sa.String(50), server_default='v1'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['raw_input_id'], ['raw_inputs.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('raw_input_id'),
    )
    op.create_table('drafts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('object_type', sa.String(50), nullable=False),
        sa.Column('source_raw_input_id', sa.Integer(), nullable=True),
        sa.Column('source_extraction_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('fields_json', sa.JSON(), nullable=True),
        sa.Column('tags_json', sa.JSON(), nullable=True),
        sa.Column('pending_questions', sa.JSON(), nullable=True),
        sa.Column('confirmed_fields', sa.JSON(), nullable=True),
        sa.Column('user_corrections', sa.JSON(), nullable=True),
        sa.Column('suggested_actions', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(50), server_default='waiting_confirmation'),
        sa.Column('formal_object_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_raw_input_id'], ['raw_inputs.id']),
        sa.ForeignKeyConstraint(['source_extraction_id'], ['input_extractions.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('learning_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('raw_input_id', sa.Integer(), nullable=True),
        sa.Column('draft_id', sa.Integer(), nullable=True),
        sa.Column('object_type', sa.String(50), nullable=False),
        sa.Column('task_type', sa.String(50), nullable=True),
        sa.Column('model_output_json', sa.JSON(), nullable=True),
        sa.Column('user_correction_json', sa.JSON(), nullable=True),
        sa.Column('final_answer_json', sa.JSON(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['raw_input_id'], ['raw_inputs.id']),
        sa.ForeignKeyConstraint(['draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('opportunities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('customer_name', sa.String(200), nullable=True),
        sa.Column('industry', sa.String(100), nullable=True),
        sa.Column('stage', sa.String(50), server_default='lead'),
        sa.Column('priority', sa.String(20), server_default='normal'),
        sa.Column('needs_summary', sa.Text(), nullable=True),
        sa.Column('decision_map', sa.JSON(), nullable=True),
        sa.Column('risk_points', sa.JSON(), nullable=True),
        sa.Column('next_actions', sa.JSON(), nullable=True),
        sa.Column('source_draft_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(20), server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('feedback_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=True),
        sa.Column('customer_name', sa.String(200), nullable=True),
        sa.Column('feedback_type', sa.String(50), nullable=True),
        sa.Column('severity', sa.String(20), server_default='medium'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('affected_module', sa.String(100), nullable=True),
        sa.Column('renewal_risk_level', sa.String(20), server_default='low'),
        sa.Column('routed_team', sa.String(100), nullable=True),
        sa.Column('knowledgeworthy', sa.Integer(), server_default='0'),
        sa.Column('source_draft_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), server_default='open'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('knowledge_entries', sa.Column('source_draft_id', sa.Integer(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('raw_input_id', sa.Integer(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('capture_mode', sa.String(50), server_default='manual_form'))
    op.add_column('messages', sa.Column('draft_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('messages', 'draft_id')
    op.drop_column('knowledge_entries', 'capture_mode')
    op.drop_column('knowledge_entries', 'raw_input_id')
    op.drop_column('knowledge_entries', 'source_draft_id')
    op.drop_table('feedback_items')
    op.drop_table('opportunities')
    op.drop_table('learning_samples')
    op.drop_table('drafts')
    op.drop_table('input_extractions')
    op.drop_table('raw_inputs')
