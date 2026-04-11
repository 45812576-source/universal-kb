"""add studio models: folder_key, skill_folder_aliases, skill_audit_results, staged_edits

Revision ID: z7a8b9c0d2a3
Revises: e1dcf23f4bbc
Create Date: 2026-04-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "z7a8b9c0d2a3"
down_revision = "e1dcf23f4bbc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. skills 表新增 folder_key
    op.add_column("skills", sa.Column("folder_key", sa.String(200), nullable=True, unique=True))

    # 2. 为现有 skill 批量生成 folder_key（基于 id）
    conn = op.get_bind()
    skills = conn.execute(sa.text("SELECT id, name FROM skills")).fetchall()
    for skill_id, name in skills:
        folder_key = f"skill-{skill_id}"
        conn.execute(
            sa.text("UPDATE skills SET folder_key = :fk WHERE id = :sid"),
            {"fk": folder_key, "sid": skill_id},
        )

    # 3. skill_folder_aliases
    op.create_table(
        "skill_folder_aliases",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("skill_id", sa.Integer, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("old_folder_key", sa.String(200), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # 4. skill_audit_results
    op.create_table(
        "skill_audit_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("skill_id", sa.Integer, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("session_id", sa.Integer, nullable=True),
        sa.Column("quality_verdict", sa.String(20)),
        sa.Column("issues", mysql.JSON, default=[]),
        sa.Column("recommended_path", sa.String(50)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # 5. staged_edits
    op.create_table(
        "staged_edits",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("skill_id", sa.Integer, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("session_id", sa.Integer, nullable=True),
        sa.Column("target_type", sa.String(30)),
        sa.Column("target_key", sa.String(200), nullable=True),
        sa.Column("diff_ops", mysql.JSON),
        sa.Column("summary", sa.Text),
        sa.Column("risk_level", sa.String(10)),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("resolved_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("staged_edits")
    op.drop_table("skill_audit_results")
    op.drop_table("skill_folder_aliases")
    op.drop_column("skills", "folder_key")
