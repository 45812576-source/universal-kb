"""approval v2 template system

- ALTER ENUM approvalrequesttype ADD 6 data safety values
- ALTER ENUM approvalactiontype ADD request_more_info, approve_with_conditions
- ALTER TABLE approval_requests ADD evidence_pack, risk_level, impact_summary
- ALTER TABLE approval_actions ADD decision_payload, checklist_result
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = "z7a8b9c0d1e8"
down_revision = "z7a8b9c0d1e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 扩展 ApprovalRequestType 枚举 ──
    # MySQL: ALTER + column redefine with full enum list
    new_request_types = (
        "'skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review',"
        "'export_sensitive','elevate_disclosure','grant_access',"
        "'policy_change','field_sensitivity_change','small_sample_change'"
    )
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN request_type "
        f"ENUM({new_request_types}) NOT NULL"
    )

    # ── 扩展 ApprovalActionType 枚举 ──
    new_action_types = "'approve','reject','add_conditions','request_more_info','approve_with_conditions'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({new_action_types}) NOT NULL"
    )

    # ── approval_requests 新增字段 ──
    op.add_column("approval_requests", sa.Column("evidence_pack", JSON, nullable=True))
    op.add_column("approval_requests", sa.Column("risk_level", sa.String(20), nullable=True))
    op.add_column("approval_requests", sa.Column("impact_summary", sa.Text, nullable=True))

    # ── approval_actions 新增字段 ──
    op.add_column("approval_actions", sa.Column("decision_payload", JSON, nullable=True))
    op.add_column("approval_actions", sa.Column("checklist_result", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("approval_actions", "checklist_result")
    op.drop_column("approval_actions", "decision_payload")
    op.drop_column("approval_requests", "impact_summary")
    op.drop_column("approval_requests", "risk_level")
    op.drop_column("approval_requests", "evidence_pack")

    # Revert enums
    old_action_types = "'approve','reject','add_conditions'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({old_action_types}) NOT NULL"
    )

    old_request_types = (
        "'skill_publish','skill_version_change','skill_ownership_transfer',"
        "'tool_publish','webapp_publish','scope_change','mask_override',"
        "'schema_approval','knowledge_edit','knowledge_review'"
    )
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN request_type "
        f"ENUM({old_request_types}) NOT NULL"
    )
