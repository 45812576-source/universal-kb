"""add approval withdraw support

- ALTER ENUM approvalstatus ADD withdrawn
- ALTER ENUM approvalactiontype ADD withdraw
"""

from alembic import op

revision = "z7a8b9c0d2a7"
down_revision = "z7a8b9c0d2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    new_status_types = "'pending','approved','rejected','conditions','withdrawn'"
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN status "
        f"ENUM({new_status_types}) NOT NULL"
    )

    new_action_types = "'approve','reject','add_conditions','request_more_info','approve_with_conditions','supplement','withdraw'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({new_action_types}) NOT NULL"
    )


def downgrade() -> None:
    old_action_types = "'approve','reject','add_conditions','request_more_info','approve_with_conditions','supplement'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({old_action_types}) NOT NULL"
    )

    old_status_types = "'pending','approved','rejected','conditions'"
    op.execute(
        f"ALTER TABLE approval_requests MODIFY COLUMN status "
        f"ENUM({old_status_types}) NOT NULL"
    )
