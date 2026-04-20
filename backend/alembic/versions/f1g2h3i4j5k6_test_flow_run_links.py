"""test flow run links and case plan draft extensions

Revision ID: f1g2h3i4j5k6
Revises: e7f8g9h0i1j2
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "f1g2h3i4j5k6"
down_revision = "e7f8g9h0i1j2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_flow_run_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("sandbox_test_reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("test_case_plan_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plan_version", sa.Integer(), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entry_source", sa.String(32), nullable=True),
        sa.Column("decision_mode", sa.String(32), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )

    op.add_column("test_case_plan_drafts", sa.Column("source_plan_id", sa.Integer(), sa.ForeignKey("test_case_plan_drafts.id", ondelete="SET NULL"), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("generation_mode", sa.String(32), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("entry_source", sa.String(32), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("conversation_id", sa.Integer(), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("summary_json", JSON(), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("latest_materialized_session_id", sa.Integer(), nullable=True))
    op.add_column("test_case_plan_drafts", sa.Column("last_used_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_case_plan_drafts", "last_used_at")
    op.drop_column("test_case_plan_drafts", "latest_materialized_session_id")
    op.drop_column("test_case_plan_drafts", "confirmed_at")
    op.drop_column("test_case_plan_drafts", "summary_json")
    op.drop_column("test_case_plan_drafts", "conversation_id")
    op.drop_column("test_case_plan_drafts", "entry_source")
    op.drop_column("test_case_plan_drafts", "generation_mode")
    op.drop_column("test_case_plan_drafts", "source_plan_id")
    op.drop_table("test_flow_run_links")
