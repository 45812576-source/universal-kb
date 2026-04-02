"""add knowledge governance foundation

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-04-02 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "governance_objectives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("level", sa.String(length=30), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("business_line", sa.String(length=100), nullable=True),
        sa.Column("objective_role", sa.String(length=50), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["governance_objectives.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_id", "code", name="uq_governance_objective_parent_code"),
    )

    op.create_table(
        "governance_department_missions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("objective_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("core_role", sa.Text(), nullable=True),
        sa.Column("mission_statement", sa.Text(), nullable=True),
        sa.Column("upstream_dependencies", sa.JSON(), nullable=True),
        sa.Column("downstream_deliverables", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["objective_id"], ["governance_objectives.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("department_id", "code", name="uq_governance_department_mission_code"),
    )

    op.create_table(
        "governance_krs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mission_id", sa.Integer(), nullable=False),
        sa.Column("objective_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metric_definition", sa.Text(), nullable=True),
        sa.Column("target_value", sa.String(length=100), nullable=True),
        sa.Column("time_horizon", sa.String(length=50), nullable=True),
        sa.Column("owner_role", sa.String(length=100), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["mission_id"], ["governance_department_missions.id"]),
        sa.ForeignKeyConstraint(["objective_id"], ["governance_objectives.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mission_id", "code", name="uq_governance_kr_mission_code"),
    )

    op.create_table(
        "governance_required_elements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kr_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("element_type", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required_library_codes", sa.JSON(), nullable=True),
        sa.Column("required_object_types", sa.JSON(), nullable=True),
        sa.Column("suggested_update_cycle", sa.String(length=30), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["kr_id"], ["governance_krs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kr_id", "code", name="uq_governance_required_element_kr_code"),
    )

    op.create_table(
        "governance_object_types",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("dimension_schema", sa.JSON(), nullable=True),
        sa.Column("baseline_fields", sa.JSON(), nullable=True),
        sa.Column("default_consumption_modes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "governance_objects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("object_type_id", sa.Integer(), nullable=False),
        sa.Column("canonical_key", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("business_line", sa.String(length=100), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=30), nullable=True),
        sa.Column("object_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["object_type_id"], ["governance_object_types.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_type_id", "canonical_key", name="uq_governance_object_type_key"),
    )

    op.create_table(
        "governance_object_facets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("governance_object_id", sa.Integer(), nullable=False),
        sa.Column("resource_library_id", sa.Integer(), nullable=False),
        sa.Column("facet_key", sa.String(length=100), nullable=False),
        sa.Column("facet_name", sa.String(length=200), nullable=False),
        sa.Column("field_values", sa.JSON(), nullable=True),
        sa.Column("consumer_scenarios", sa.JSON(), nullable=True),
        sa.Column("visibility_mode", sa.String(length=20), nullable=True),
        sa.Column("is_editable", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("update_cycle", sa.String(length=30), nullable=True),
        sa.Column("source_subjects", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["governance_object_id"], ["governance_objects.id"]),
        sa.ForeignKeyConstraint(["resource_library_id"], ["governance_resource_libraries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("governance_object_id", "resource_library_id", "facet_key", name="uq_governance_object_facet"),
    )

    op.create_table(
        "governance_resource_libraries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("objective_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("library_type", sa.String(length=50), nullable=True),
        sa.Column("object_type", sa.String(length=50), nullable=False),
        sa.Column("governance_mode", sa.String(length=20), nullable=True),
        sa.Column("default_visibility", sa.String(length=20), nullable=True),
        sa.Column("default_update_cycle", sa.String(length=30), nullable=True),
        sa.Column("field_schema", sa.JSON(), nullable=True),
        sa.Column("consumption_scenarios", sa.JSON(), nullable=True),
        sa.Column("collaboration_baseline", sa.JSON(), nullable=True),
        sa.Column("classification_hints", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["objective_id"], ["governance_objectives.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("objective_id", "code", name="uq_governance_library_objective_code"),
    )

    op.create_table(
        "governance_field_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("object_type_id", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=100), nullable=False),
        sa.Column("field_label", sa.String(length=200), nullable=False),
        sa.Column("field_type", sa.String(length=50), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_editable", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("visibility_mode", sa.String(length=20), nullable=True),
        sa.Column("update_cycle", sa.String(length=30), nullable=True),
        sa.Column("consumer_modes", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("example_values", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["object_type_id"], ["governance_object_types.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_type_id", "field_key", name="uq_governance_field_template_object_field"),
    )

    op.create_table(
        "governance_suggestion_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_type", sa.String(length=50), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("objective_id", sa.Integer(), nullable=True),
        sa.Column("resource_library_id", sa.Integer(), nullable=True),
        sa.Column("object_type_id", sa.Integer(), nullable=True),
        sa.Column("suggested_payload", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["objective_id"], ["governance_objectives.id"]),
        sa.ForeignKeyConstraint(["object_type_id"], ["governance_object_types.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["resource_library_id"], ["governance_resource_libraries.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "governance_feedback_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=True),
        sa.Column("subject_type", sa.String(length=50), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("strategy_key", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("reward_score", sa.Integer(), nullable=True),
        sa.Column("from_objective_id", sa.Integer(), nullable=True),
        sa.Column("from_resource_library_id", sa.Integer(), nullable=True),
        sa.Column("to_objective_id", sa.Integer(), nullable=True),
        sa.Column("to_resource_library_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["suggestion_id"], ["governance_suggestion_tasks.id"]),
        sa.ForeignKeyConstraint(["from_objective_id"], ["governance_objectives.id"]),
        sa.ForeignKeyConstraint(["from_resource_library_id"], ["governance_resource_libraries.id"]),
        sa.ForeignKeyConstraint(["to_objective_id"], ["governance_objectives.id"]),
        sa.ForeignKeyConstraint(["to_resource_library_id"], ["governance_resource_libraries.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "governance_strategy_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_key", sa.String(length=200), nullable=False),
        sa.Column("strategy_group", sa.String(length=100), nullable=False),
        sa.Column("subject_type", sa.String(length=50), nullable=True),
        sa.Column("objective_code", sa.String(length=100), nullable=True),
        sa.Column("library_code", sa.String(length=100), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=True),
        sa.Column("reject_count", sa.Integer(), nullable=True),
        sa.Column("cumulative_reward", sa.Integer(), nullable=True),
        sa.Column("last_reward", sa.Integer(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_key", name="uq_governance_strategy_key"),
    )

    op.add_column("knowledge_entries", sa.Column("governance_objective_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("resource_library_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("object_type_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("governance_object_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("governance_kr_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("governance_element_id", sa.Integer(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("governance_status", sa.String(length=20), nullable=True, server_default="ungoverned"))
    op.add_column("knowledge_entries", sa.Column("governance_confidence", sa.Float(), nullable=True))
    op.add_column("knowledge_entries", sa.Column("governance_note", sa.Text(), nullable=True))
    op.create_foreign_key(None, "knowledge_entries", "governance_objectives", ["governance_objective_id"], ["id"])
    op.create_foreign_key(None, "knowledge_entries", "governance_resource_libraries", ["resource_library_id"], ["id"])
    op.create_foreign_key(None, "knowledge_entries", "governance_object_types", ["object_type_id"], ["id"])
    op.create_foreign_key(None, "knowledge_entries", "governance_objects", ["governance_object_id"], ["id"])
    op.create_foreign_key(None, "knowledge_entries", "governance_krs", ["governance_kr_id"], ["id"])
    op.create_foreign_key(None, "knowledge_entries", "governance_required_elements", ["governance_element_id"], ["id"])

    op.add_column("business_tables", sa.Column("governance_objective_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("resource_library_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("object_type_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("governance_object_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("governance_kr_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("governance_element_id", sa.Integer(), nullable=True))
    op.add_column("business_tables", sa.Column("governance_status", sa.String(length=20), nullable=True, server_default="ungoverned"))
    op.add_column("business_tables", sa.Column("governance_note", sa.Text(), nullable=True))
    op.create_foreign_key(None, "business_tables", "governance_objectives", ["governance_objective_id"], ["id"])
    op.create_foreign_key(None, "business_tables", "governance_resource_libraries", ["resource_library_id"], ["id"])
    op.create_foreign_key(None, "business_tables", "governance_object_types", ["object_type_id"], ["id"])
    op.create_foreign_key(None, "business_tables", "governance_objects", ["governance_object_id"], ["id"])
    op.create_foreign_key(None, "business_tables", "governance_krs", ["governance_kr_id"], ["id"])
    op.create_foreign_key(None, "business_tables", "governance_required_elements", ["governance_element_id"], ["id"])

    op.add_column("projects", sa.Column("governance_objective_id", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("resource_library_ids", sa.JSON(), nullable=True))
    op.add_column("projects", sa.Column("governance_kr_id", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("governance_note", sa.Text(), nullable=True))
    op.create_foreign_key(None, "projects", "governance_objectives", ["governance_objective_id"], ["id"])
    op.create_foreign_key(None, "projects", "governance_krs", ["governance_kr_id"], ["id"])

    op.add_column("tasks", sa.Column("governance_objective_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("resource_library_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("governance_kr_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("governance_object_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("object_anchor", sa.String(length=100), nullable=True))
    op.create_foreign_key(None, "tasks", "governance_objectives", ["governance_objective_id"], ["id"])
    op.create_foreign_key(None, "tasks", "governance_resource_libraries", ["resource_library_id"], ["id"])
    op.create_foreign_key(None, "tasks", "governance_krs", ["governance_kr_id"], ["id"])
    op.create_foreign_key(None, "tasks", "governance_objects", ["governance_object_id"], ["id"])


def downgrade() -> None:
    op.drop_table("governance_strategy_stats")
    op.drop_table("governance_feedback_events")
    op.drop_constraint(None, "tasks", type_="foreignkey")
    op.drop_constraint(None, "tasks", type_="foreignkey")
    op.drop_constraint(None, "tasks", type_="foreignkey")
    op.drop_constraint(None, "tasks", type_="foreignkey")
    op.drop_column("tasks", "object_anchor")
    op.drop_column("tasks", "governance_object_id")
    op.drop_column("tasks", "governance_kr_id")
    op.drop_column("tasks", "resource_library_id")
    op.drop_column("tasks", "governance_objective_id")

    op.drop_constraint(None, "projects", type_="foreignkey")
    op.drop_constraint(None, "projects", type_="foreignkey")
    op.drop_column("projects", "governance_note")
    op.drop_column("projects", "governance_kr_id")
    op.drop_column("projects", "resource_library_ids")
    op.drop_column("projects", "governance_objective_id")

    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_constraint(None, "business_tables", type_="foreignkey")
    op.drop_column("business_tables", "governance_note")
    op.drop_column("business_tables", "governance_status")
    op.drop_column("business_tables", "governance_element_id")
    op.drop_column("business_tables", "governance_kr_id")
    op.drop_column("business_tables", "governance_object_id")
    op.drop_column("business_tables", "object_type_id")
    op.drop_column("business_tables", "resource_library_id")
    op.drop_column("business_tables", "governance_objective_id")

    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_constraint(None, "knowledge_entries", type_="foreignkey")
    op.drop_column("knowledge_entries", "governance_note")
    op.drop_column("knowledge_entries", "governance_confidence")
    op.drop_column("knowledge_entries", "governance_status")
    op.drop_column("knowledge_entries", "governance_element_id")
    op.drop_column("knowledge_entries", "governance_kr_id")
    op.drop_column("knowledge_entries", "governance_object_id")
    op.drop_column("knowledge_entries", "object_type_id")
    op.drop_column("knowledge_entries", "resource_library_id")
    op.drop_column("knowledge_entries", "governance_objective_id")

    op.drop_table("governance_suggestion_tasks")
    op.drop_table("governance_field_templates")
    op.drop_table("governance_object_facets")
    op.drop_table("governance_objects")
    op.drop_table("governance_resource_libraries")
    op.drop_table("governance_object_types")
    op.drop_table("governance_required_elements")
    op.drop_table("governance_krs")
    op.drop_table("governance_department_missions")
    op.drop_table("governance_objectives")
