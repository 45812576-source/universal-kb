"""交互式沙盒测试: sandbox_test_sessions / evidences / cases / reports

Revision ID: a1b2c3d4e5f6
Revises: z7a8b9c0d1e2
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "a1b2c3d4e5f6"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── sandbox_test_reports (先建，因为 sessions 有 FK 指向它) ───────────
    op.create_table(
        "sandbox_test_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("target_name", sa.String(200), nullable=True),
        sa.Column("tester_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("part1_evidence_check", mysql.JSON(), nullable=True),
        sa.Column("part2_test_matrix", mysql.JSON(), nullable=True),
        sa.Column("part3_evaluation", mysql.JSON(), nullable=True),
        sa.Column("theoretical_combo_count", sa.Integer(), nullable=True),
        sa.Column("semantic_combo_count", sa.Integer(), nullable=True),
        sa.Column("executed_case_count", sa.Integer(), nullable=True),
        sa.Column("quality_passed", sa.Boolean(), nullable=True),
        sa.Column("usability_passed", sa.Boolean(), nullable=True),
        sa.Column("anti_hallucination_passed", sa.Boolean(), nullable=True),
        sa.Column("approval_eligible", sa.Boolean(), nullable=True),
        sa.Column("report_hash", sa.String(64), nullable=True),
        sa.Column("knowledge_entry_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # ── sandbox_test_sessions ────────────────────────────────────────────
    op.create_table(
        "sandbox_test_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("target_name", sa.String(200), nullable=True),
        sa.Column("tester_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Enum("draft", "blocked", "ready_to_run", "running", "completed", "cannot_test", name="session_status"), nullable=False, server_default="draft"),
        sa.Column("current_step", sa.Enum("start", "input_slot_review", "tool_review", "permission_review", "case_generation", "execution", "evaluation", "done", name="session_step"), nullable=False, server_default="start"),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("detected_slots", mysql.JSON(), nullable=True),
        sa.Column("tool_review", mysql.JSON(), nullable=True),
        sa.Column("permission_snapshot", mysql.JSON(), nullable=True),
        sa.Column("theoretical_combo_count", sa.Integer(), nullable=True),
        sa.Column("semantic_combo_count", sa.Integer(), nullable=True),
        sa.Column("executed_case_count", sa.Integer(), nullable=True),
        sa.Column("quality_passed", sa.Boolean(), nullable=True),
        sa.Column("usability_passed", sa.Boolean(), nullable=True),
        sa.Column("anti_hallucination_passed", sa.Boolean(), nullable=True),
        sa.Column("approval_eligible", sa.Boolean(), nullable=True),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("sandbox_test_reports.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )

    # 反向 FK: reports.session_id → sessions.id
    op.create_foreign_key("fk_report_session", "sandbox_test_reports", "sandbox_test_sessions", ["session_id"], ["id"])

    # ── sandbox_test_evidences ───────────────────────────────────────────
    op.create_table(
        "sandbox_test_evidences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id"), nullable=False),
        sa.Column("evidence_type", sa.Enum("input_slot", "knowledge_binding", "rag_sample", "tool_provenance", "permission_snapshot", name="evidence_type"), nullable=False),
        sa.Column("step", sa.String(30), nullable=False),
        sa.Column("slot_key", sa.String(100), nullable=True),
        sa.Column("source_kind", sa.Enum("chat_text", "knowledge", "data_table", "system_runtime", name="slot_source_kind"), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("resolved_value_preview", sa.Text(), nullable=True),
        sa.Column("knowledge_entry_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id"), nullable=True),
        sa.Column("rag_query", sa.Text(), nullable=True),
        sa.Column("rag_expected_ids", mysql.JSON(), nullable=True),
        sa.Column("rag_actual_hits", mysql.JSON(), nullable=True),
        sa.Column("rag_hit", sa.Boolean(), nullable=True),
        sa.Column("tool_id", sa.Integer(), sa.ForeignKey("tool_registry.id"), nullable=True),
        sa.Column("field_name", sa.String(100), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=True),
        sa.Column("table_name", sa.String(100), nullable=True),
        sa.Column("snapshot_data", mysql.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # ── sandbox_test_cases ───────────────────────────────────────────────
    op.create_table(
        "sandbox_test_cases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id"), nullable=False),
        sa.Column("case_index", sa.Integer(), nullable=False),
        sa.Column("row_visibility", sa.String(20), nullable=True),
        sa.Column("field_output_semantic", sa.String(50), nullable=True),
        sa.Column("group_semantic", sa.String(50), nullable=True),
        sa.Column("tool_precondition", sa.String(50), nullable=True),
        sa.Column("input_provenance", mysql.JSON(), nullable=True),
        sa.Column("test_input", sa.Text(), nullable=True),
        sa.Column("system_prompt_used", sa.Text(), nullable=True),
        sa.Column("llm_response", sa.Text(), nullable=True),
        sa.Column("execution_duration_ms", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.Enum("passed", "failed", "error", "skipped", name="case_verdict"), nullable=True),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sandbox_test_cases")
    op.drop_table("sandbox_test_evidences")
    op.drop_constraint("fk_report_session", "sandbox_test_reports", type_="foreignkey")
    op.drop_table("sandbox_test_sessions")
    op.drop_table("sandbox_test_reports")
