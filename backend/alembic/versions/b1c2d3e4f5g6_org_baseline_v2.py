"""org baseline v2: baseline center + 5 foundation tables

- org_baselines: 组织基线版本中心
- position_competency_models: 岗位能力模型
- resource_library_definitions: 资源库定义中心
- kr_resource_mappings: KR→资源库映射
- collab_protocols: 协同协议基线
- org_change_impacts: 变更影响分析

Revision ID: b1c2d3e4f5g6
Revises: a0b1c2d3e4f5
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision: str = "b1c2d3e4f5g6"
down_revision: Union[str, Sequence[str], None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_baselines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(20), unique=True, nullable=False),
        sa.Column("version_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=True),
        sa.Column("snapshot_summary", JSON(), nullable=True),
        sa.Column("diff_from_previous", JSON(), nullable=True),
        sa.Column("impact_analysis", JSON(), nullable=True),
        sa.Column("governance_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("trigger_source", sa.String(30), server_default="manual", nullable=True),
        sa.Column("trigger_import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("activated_by", sa.Integer(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["governance_snapshot_id"], ["governance_baseline_snapshots.id"]),
        sa.ForeignKeyConstraint(["trigger_import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["activated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "position_competency_models",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("responsibilities", JSON(), nullable=True),
        sa.Column("competencies", JSON(), nullable=True),
        sa.Column("output_standards", JSON(), nullable=True),
        sa.Column("career_path", JSON(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("position_id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "resource_library_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("library_code", sa.String(100), unique=True, nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("owner_department_id", sa.Integer(), nullable=True),
        sa.Column("owner_position_id", sa.Integer(), nullable=True),
        sa.Column("required_fields", JSON(), nullable=True),
        sa.Column("consumption_scenarios", JSON(), nullable=True),
        sa.Column("read_write_policy", JSON(), nullable=True),
        sa.Column("update_cycle_sla", sa.String(30), nullable=True),
        sa.Column("quality_baseline", JSON(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["owner_position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "kr_resource_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kr_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(30), nullable=False),
        sa.Column("target_code", sa.String(100), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("relevance", sa.String(20), server_default="direct", nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["kr_id"], ["okr_key_results.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("kr_id", "target_type", "target_code", name="uq_kr_resource_mapping"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "collab_protocols",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_department_id", sa.Integer(), nullable=False),
        sa.Column("consumer_department_id", sa.Integer(), nullable=False),
        sa.Column("data_object", sa.String(200), nullable=False),
        sa.Column("provider_position_id", sa.Integer(), nullable=True),
        sa.Column("consumer_position_id", sa.Integer(), nullable=True),
        sa.Column("trigger_event", sa.String(200), nullable=True),
        sa.Column("sync_frequency", sa.String(20), server_default="manual", nullable=True),
        sa.Column("latency_tolerance", sa.String(50), nullable=True),
        sa.Column("sla_description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=True),
        sa.Column("import_session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["provider_department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["consumer_department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["provider_position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["consumer_position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["import_session_id"], ["org_import_sessions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "org_change_impacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("baseline_id", sa.Integer(), nullable=False),
        sa.Column("change_event_id", sa.Integer(), nullable=True),
        sa.Column("impact_type", sa.String(50), nullable=False),
        sa.Column("impact_target_type", sa.String(50), nullable=False),
        sa.Column("impact_target_id", sa.Integer(), nullable=True),
        sa.Column("impact_target_name", sa.String(200), nullable=True),
        sa.Column("severity", sa.String(10), server_default="medium", nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("0"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["baseline_id"], ["org_baselines.id"]),
        sa.ForeignKeyConstraint(["change_event_id"], ["org_change_events.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_org_change_impacts_baseline", "org_change_impacts", ["baseline_id", "resolved"])


def downgrade() -> None:
    op.drop_index("ix_org_change_impacts_baseline", "org_change_impacts")
    op.drop_table("org_change_impacts")
    op.drop_table("collab_protocols")
    op.drop_table("kr_resource_mappings")
    op.drop_table("resource_library_definitions")
    op.drop_table("position_competency_models")
    op.drop_table("org_baselines")
