"""add asset preview enrichment fields

Revision ID: 202607040001
Revises: 202607030002
Create Date: 2026-07-04 03:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607040001"
down_revision: str | Sequence[str] | None = "202607030002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column(
            "preview_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("preview_path", sa.String(length=1200), nullable=True))
    op.add_column("assets", sa.Column("thumbnail_path", sa.String(length=1200), nullable=True))
    op.add_column(
        "assets",
        sa.Column("preview_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("assets", sa.Column("preview_error", sa.Text(), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "technical_metadata_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("probe_error", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("fps", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("codec", sa.String(length=120), nullable=True))
    op.add_column("assets", sa.Column("has_audio", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_assets_preview_status",
        "assets",
        "preview_status in ('pending', 'processing', 'ready', 'failed', 'skipped')",
    )
    op.create_check_constraint(
        "ck_assets_technical_metadata_status",
        "assets",
        (
            "technical_metadata_status in "
            "('pending', 'processing', 'ready', 'failed', 'skipped')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_assets_technical_metadata_status", "assets", type_="check")
    op.drop_constraint("ck_assets_preview_status", "assets", type_="check")
    op.drop_column("assets", "has_audio")
    op.drop_column("assets", "codec")
    op.drop_column("assets", "fps")
    op.drop_column("assets", "probe_error")
    op.drop_column("assets", "technical_metadata_status")
    op.drop_column("assets", "preview_error")
    op.drop_column("assets", "preview_generated_at")
    op.drop_column("assets", "thumbnail_path")
    op.drop_column("assets", "preview_path")
    op.drop_column("assets", "preview_status")
