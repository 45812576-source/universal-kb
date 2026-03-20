"""add scope/status/department_id/saved to tool_registry

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'q7r8s9t0u1v2'
down_revision = ('p6q7r8s9t0u1', 'g8h9i0j1k2l3')
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tool_registry', sa.Column('scope', sa.String(20), server_default='personal', nullable=False))
    op.add_column('tool_registry', sa.Column('status', sa.String(20), server_default='draft', nullable=False))
    op.add_column('tool_registry', sa.Column('department_id', sa.Integer, sa.ForeignKey('departments.id'), nullable=True))
    op.create_table(
        'user_saved_tools',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('tool_id', sa.Integer, sa.ForeignKey('tool_registry.id'), nullable=False),
        sa.Column('saved_at', sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'tool_id', name='uq_user_saved_tool'),
    )


def downgrade():
    op.drop_table('user_saved_tools')
    op.drop_column('tool_registry', 'department_id')
    op.drop_column('tool_registry', 'status')
    op.drop_column('tool_registry', 'scope')
