"""Add verification_required managed Drive move status.

Revision ID: 202608040001
Revises: 202608030001
Create Date: 2026-08-04 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "202608040001"
down_revision = "202608030001"
branch_labels = None
depends_on = None


NEW_MOVE_STATUS_VALUES = (
    "'not_planned'",
    "'planned'",
    "'moving'",
    "'moved'",
    "'verification_required'",
    "'rollback_required'",
    "'move_failed'",
)
OLD_MOVE_STATUS_VALUES = (
    "'not_planned'",
    "'planned'",
    "'moving'",
    "'moved'",
    "'rollback_required'",
    "'move_failed'",
)


def upgrade() -> None:
    op.drop_constraint("ck_assets_move_status", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_move_status",
        "assets",
        f"move_status in ({', '.join(NEW_MOVE_STATUS_VALUES)})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_assets_move_status", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_move_status",
        "assets",
        f"move_status in ({', '.join(OLD_MOVE_STATUS_VALUES)})",
    )
