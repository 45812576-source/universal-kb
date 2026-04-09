"""sandbox v2: step_statuses, parent_session_id, rerun_scope

如果 sandbox 表不存在（原始 migration 未执行），则先建表再加字段。

Revision ID: b2c3d4e5f6a8
Revises: c3d4e5f6a7b9
Create Date: 2026-04-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    # 如果 sandbox 表不存在，先建基础表
    if not _table_exists("sandbox_test_reports"):
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

    if not _table_exists("sandbox_test_sessions"):
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
            # V2 新增字段直接建入
            sa.Column("step_statuses", mysql.JSON(), nullable=True),
            sa.Column("parent_session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id"), nullable=True),
            sa.Column("rerun_scope", mysql.JSON(), nullable=True),
        )
        op.create_foreign_key("fk_report_session", "sandbox_test_reports", "sandbox_test_sessions", ["session_id"], ["id"])
    else:
        # 表已存在，只加新字段
        op.add_column("sandbox_test_sessions", sa.Column("step_statuses", mysql.JSON(), nullable=True))
        op.add_column("sandbox_test_sessions", sa.Column("parent_session_id", sa.Integer(), sa.ForeignKey("sandbox_test_sessions.id"), nullable=True))
        op.add_column("sandbox_test_sessions", sa.Column("rerun_scope", mysql.JSON(), nullable=True))

    if not _table_exists("sandbox_test_evidences"):
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

    if not _table_exists("sandbox_test_cases"):
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
    if _table_exists("sandbox_test_sessions"):
        try:
            op.drop_column("sandbox_test_sessions", "rerun_scope")
            op.drop_column("sandbox_test_sessions", "parent_session_id")
            op.drop_column("sandbox_test_sessions", "step_statuses")
        except Exception:
            pass
