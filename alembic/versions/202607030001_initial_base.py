"""initial base

Revision ID: 202607030001
Revises:
Create Date: 2026-07-03 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202607030001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SELECT 1")


def downgrade() -> None:
    op.execute("SELECT 1")
