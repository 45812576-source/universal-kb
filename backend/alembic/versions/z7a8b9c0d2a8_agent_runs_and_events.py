"""agent_runs and agent_run_events tables

Phase B1/B2: DB-backed run lifecycle + append-only event log for Skill Studio.
"""

import sqlalchemy as sa
from alembic import op

revision = "z7a8b9c0d2a8"
down_revision = "z7a8b9c0d2a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("public_run_id", sa.String(64), nullable=False, unique=True),
        sa.Column("harness_run_id", sa.String(64), nullable=True),
        sa.Column("parent_run_id", sa.String(64), nullable=True),
        sa.Column("conversation_id", sa.Integer, nullable=False),
        sa.Column("skill_id", sa.Integer, nullable=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("active_card_id", sa.String(64), nullable=True),
        sa.Column("run_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("superseded_by", sa.String(64), nullable=True),
        sa.Column("message_id", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("cancelled_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_public_run_id", "agent_runs", ["public_run_id"], unique=True)
    op.create_index("ix_agent_runs_harness_run_id", "agent_runs", ["harness_run_id"])
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.create_index("ix_agent_runs_skill_id", "agent_runs", ["skill_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_conv_status", "agent_runs", ["conversation_id", "status"])

    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("public_run_id", sa.String(64), nullable=False),
        sa.Column("run_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("harness_run_id", sa.String(64), nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("patch_type", sa.String(64), nullable=True),
        sa.Column("payload_json", sa.JSON, default=dict),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_run_events_public_run_id", "agent_run_events", ["public_run_id"])
    op.create_index("ix_agent_run_events_event_type", "agent_run_events", ["event_type"])
    op.create_index("ix_agent_run_events_run_seq", "agent_run_events", ["public_run_id", "sequence"])
    op.create_index("ix_agent_run_events_created_at", "agent_run_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("agent_run_events")
    op.drop_table("agent_runs")
