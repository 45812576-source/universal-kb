"""project chat: add project_id to conversations and tasks

Revision ID: t1u2v3w4x5y6
Revises: s9t0u1v2w3x4
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = 't1u2v3w4x5y6'
down_revision = 's9t0u1v2w3x4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('conversations', sa.Column('project_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_conv_project', 'conversations', 'projects', ['project_id'], ['id'],
        ondelete='SET NULL'
    )

    op.add_column('tasks', sa.Column('project_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_task_project', 'tasks', 'projects', ['project_id'], ['id'],
        ondelete='SET NULL'
    )


def downgrade():
    op.drop_constraint('fk_task_project', 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'project_id')

    op.drop_constraint('fk_conv_project', 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'project_id')
