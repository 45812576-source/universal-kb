"""web_app backend process fields

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'u2v3w4x5y6z7'
down_revision = 't1u2v3w4x5y6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('web_apps', sa.Column('backend_cmd', sa.Text(), nullable=True))
    op.add_column('web_apps', sa.Column('backend_cwd', sa.Text(), nullable=True))
    op.add_column('web_apps', sa.Column('backend_port', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('web_apps', 'backend_port')
    op.drop_column('web_apps', 'backend_cwd')
    op.drop_column('web_apps', 'backend_cmd')
