"""add supplement action type

- ALTER ENUM approvalactiontype ADD supplement
"""

from alembic import op


def upgrade() -> None:
    new_action_types = "'approve','reject','add_conditions','request_more_info','approve_with_conditions','supplement'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({new_action_types}) NOT NULL"
    )


def downgrade() -> None:
    old_action_types = "'approve','reject','add_conditions','request_more_info','approve_with_conditions'"
    op.execute(
        f"ALTER TABLE approval_actions MODIFY COLUMN action "
        f"ENUM({old_action_types}) NOT NULL"
    )
