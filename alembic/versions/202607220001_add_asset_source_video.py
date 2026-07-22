"""add asset source video

Revision ID: 202607220001
Revises: 202607210002
Create Date: 2026-07-22 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607220001"
down_revision: str | Sequence[str] | None = "202607210002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("source_video", sa.String(length=1200), nullable=True))
    op.create_index("ix_assets_source_video", "assets", ["source_video"])


def downgrade() -> None:
    op.drop_index("ix_assets_source_video", table_name="assets")
    op.drop_column("assets", "source_video")
