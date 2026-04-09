"""add org management module

- 4 张表增字段 (departments, users, positions, governance_department_missions)
- 11 张新表 (org_import_sessions, org_change_events, okr_periods, okr_objectives,
  okr_key_results, kpi_assignments, dept_mission_details, biz_processes,
  biz_terminologies, data_asset_ownerships, dept_collaboration_links,
  position_access_rules)

Revision ID: a0b1c2d3e4f5
Revises: z7a8b9c0d2a0
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "z7a8b9c0d2a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. 增强现有表 ────────────────────────────────────────────────────────

    # departments 增字段
    op.add_column("departments", sa.Column("code", sa.String(50), unique=True, nullable=True))
    op.add_column("departments", sa.Column("level", sa.String(30), nullable=True))
    op.add_column("departments", sa.Column("headcount_budget", sa.Integer(), nullable=True))
    op.add_column("departments", sa.Column("lifecycle_status", sa.String(20), server_default="active", nullable=True))
    op.add_column("departments", sa.Column("established_at", sa.Date(), nullable=True))
    op.add_column("departments", sa.Column("dissolved_at", sa.Date(), nullable=True))
    op.add_column("departments", sa.Column("sort_order", sa.Integer(), server_default="0", nullable=True))

    # users 增字段
    op.add_column("users", sa.Column("employee_no", sa.String(50), unique=True, nullable=True))
    op.add_column("users", sa.Column("employee_status", sa.String(20), server_default="active", nullable=True))
    op.add_column("users", sa.Column("job_title", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("job_level", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("entry_date", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("exit_date", sa.Date(), nullable=True))

    # positions 增字段
    op.add_column("positions", sa.Column("code", sa.String(50), unique=True, nullable=True))
    op.add_column("positions", sa.Column("kpi_template", JSON(), nullable=True))
    op.add_column("positions", sa.Column("evaluation_cycle", sa.String(20), nullable=True))
    op.add_column("positions", sa.Column("required_data_domains", JSON(), nullable=True))
    op.add_column("positions", sa.Column("deliverables", JSON(), nullable=True))
    op.add_column("positions", sa.Column("sort_order", sa.Integer(), server_default="0", nullable=True))

    # governance_department_missions 增字段
    op.add_column("governance_department_missions", sa.Column("source", sa.String(20), server_default="ai", nullable=True))
    op.add_column("governance_department_missions", sa.Column("confirmed_by", sa.Integer(), nullable=True))
    op.add_column("governance_department_missions", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.create_foreign_key("fk_gdm_confirmed_by", "governance_department_missions", "users", ["confirmed_by"], ["id"])

    # ── 2. 新建表 ────────────────────────────────────────────────────────────

    op.create_table(
        "org_import_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_type", sa.String(30), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("raw_data", JSON(), nullable=True),
        sa.Column("ai_parsed_data", JSON(), nullable=True),
        sa.Column("ai_parse_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default="uploading", nullable=True),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=True),
        sa.Column("parsed_count", sa.Integer(), server_default="0", nullable=True),
        sa.Column("error_rows", JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("baseline_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["baseline_snapshot_id"], ["governance_baseline_snapshots.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "org_change_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("field_changes", JSON(), nullable=True),
        sa.Column("change_source", sa.String(20), server_default="manual", nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("baseline_version", sa.String(20), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "okr_periods",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("period_type", sa.String(20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "okr_objectives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("owner_type", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.Integer(), server_default="0", nullable=True),
        sa.Column("parent_objective_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=True),
        sa.Column("progress", sa.Float(), server_default="0", nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["period_id"], ["okr_periods.id"]),
        sa.ForeignKeyConstraint(["parent_objective_id"], ["okr_objectives.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "okr_key_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("objective_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("metric_type", sa.String(20), server_default="number", nullable=True),
        sa.Column("target_value", sa.String(100), nullable=True),
        sa.Column("current_value", sa.String(100), nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=True),
        sa.Column("progress", sa.Float(), server_default="0", nullable=True),
        sa.Column("status", sa.String(20), server_default="on_track", nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["objective_id"], ["okr_objectives.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "kpi_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("kpi_data", JSON(), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("level", sa.String(10), nullable=True),
        sa.Column("evaluator_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["period_id"], ["okr_periods.id"]),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["evaluator_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "dept_mission_details",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("mission_summary", sa.Text(), nullable=True),
        sa.Column("core_functions", JSON(), nullable=True),
        sa.Column("upstream_deps", JSON(), nullable=True),
        sa.Column("downstream_deliveries", JSON(), nullable=True),
        sa.Column("owned_data_types", JSON(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("department_id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "biz_processes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("process_nodes", JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("code"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "biz_terminologies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("term", sa.String(200), nullable=False),
        sa.Column("aliases", JSON(), nullable=True),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("resource_library_code", sa.String(100), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "data_asset_ownerships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_name", sa.String(200), nullable=False),
        sa.Column("asset_code", sa.String(100), nullable=False),
        sa.Column("owner_department_id", sa.Integer(), nullable=False),
        sa.Column("update_frequency", sa.String(20), server_default="manual", nullable=True),
        sa.Column("consumer_department_ids", JSON(), nullable=True),
        sa.Column("resource_library_code", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.UniqueConstraint("asset_code"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "dept_collaboration_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dept_a_id", sa.Integer(), nullable=False),
        sa.Column("dept_b_id", sa.Integer(), nullable=False),
        sa.Column("frequency", sa.String(10), server_default="medium", nullable=True),
        sa.Column("scenarios", JSON(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dept_a_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["dept_b_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "position_access_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("data_domain", sa.String(50), nullable=False),
        sa.Column("access_range", sa.String(20), server_default="none", nullable=True),
        sa.Column("excluded_fields", JSON(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.UniqueConstraint("position_id", "data_domain", name="uq_position_access_rule"),
        sa.PrimaryKeyConstraint("id"),
    )

    # 索引
    op.create_index("ix_org_change_entity", "org_change_events", ["entity_type", "entity_id"])
    op.create_index("ix_org_change_created", "org_change_events", ["created_at"])
    op.create_index("ix_okr_objectives_period", "okr_objectives", ["period_id"])
    op.create_index("ix_kpi_assignments_user", "kpi_assignments", ["user_id", "period_id"])


def downgrade() -> None:
    # 删除索引
    op.drop_index("ix_kpi_assignments_user", "kpi_assignments")
    op.drop_index("ix_okr_objectives_period", "okr_objectives")
    op.drop_index("ix_org_change_created", "org_change_events")
    op.drop_index("ix_org_change_entity", "org_change_events")

    # 删除新表（逆序，先删有 FK 依赖的）
    op.drop_table("position_access_rules")
    op.drop_table("dept_collaboration_links")
    op.drop_table("data_asset_ownerships")
    op.drop_table("biz_terminologies")
    op.drop_table("biz_processes")
    op.drop_table("dept_mission_details")
    op.drop_table("kpi_assignments")
    op.drop_table("okr_key_results")
    op.drop_table("okr_objectives")
    op.drop_table("okr_periods")
    op.drop_table("org_change_events")
    op.drop_table("org_import_sessions")

    # 删除增强字段
    op.drop_constraint("fk_gdm_confirmed_by", "governance_department_missions", type_="foreignkey")
    op.drop_column("governance_department_missions", "confirmed_at")
    op.drop_column("governance_department_missions", "confirmed_by")
    op.drop_column("governance_department_missions", "source")

    op.drop_column("positions", "sort_order")
    op.drop_column("positions", "deliverables")
    op.drop_column("positions", "required_data_domains")
    op.drop_column("positions", "evaluation_cycle")
    op.drop_column("positions", "kpi_template")
    op.drop_column("positions", "code")

    op.drop_column("users", "exit_date")
    op.drop_column("users", "entry_date")
    op.drop_column("users", "job_level")
    op.drop_column("users", "job_title")
    op.drop_column("users", "employee_status")
    op.drop_column("users", "employee_no")

    op.drop_column("departments", "sort_order")
    op.drop_column("departments", "dissolved_at")
    op.drop_column("departments", "established_at")
    op.drop_column("departments", "lifecycle_status")
    op.drop_column("departments", "headcount_budget")
    op.drop_column("departments", "level")
    op.drop_column("departments", "code")
