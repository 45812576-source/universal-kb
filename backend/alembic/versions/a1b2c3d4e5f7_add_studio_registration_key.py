"""add studio registration key dimensions

Revision ID: a1b2c3d4e5f7
Revises: z7a8b9c0d2a2
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f7"
down_revision = "z7a8b9c0d2a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("studio_instance_registrations") as batch:
        batch.drop_constraint("uq_user_workspace_type", type_="unique")
        batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("target_type", sa.String(50), nullable=True))
        batch.add_column(sa.Column("target_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("registration_key", sa.String(255), nullable=False, server_default="default"))
        batch.create_foreign_key(
            "fk_studio_registration_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_studio_registration_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_user_workspace_registration_key",
            ["user_id", "workspace_type", "registration_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("studio_instance_registrations") as batch:
        batch.drop_constraint("uq_user_workspace_registration_key", type_="unique")
        batch.drop_constraint("fk_studio_registration_project_id", type_="foreignkey")
        batch.drop_constraint("fk_studio_registration_workspace_id", type_="foreignkey")
        batch.drop_column("registration_key")
        batch.drop_column("target_id")
        batch.drop_column("target_type")
        batch.drop_column("project_id")
        batch.drop_column("workspace_id")
        batch.create_unique_constraint(
            "uq_user_workspace_type",
            ["user_id", "workspace_type"],
        )
