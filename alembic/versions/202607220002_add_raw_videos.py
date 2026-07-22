"""add raw videos control table

Revision ID: 202607220002
Revises: 202607220001
Create Date: 2026-07-22 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607220002"
down_revision: str | Sequence[str] | None = "202607220001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_videos",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("remote_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column(
            "provider",
            sa.Text(),
            server_default=sa.text("'gdrive_people_raw_long'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("segments_generated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('NEW', 'PROCESSING', 'DONE', 'FAILED')",
            name="ck_raw_videos_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_path", name="uq_raw_videos_remote_path"),
    )
    op.create_index("ix_raw_videos_status", "raw_videos", ["status"])
    op.create_index("ix_raw_videos_last_seen_at", "raw_videos", ["last_seen_at"])

    op.add_column("assets", sa.Column("raw_video_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_assets_raw_video_id_raw_videos",
        "assets",
        "raw_videos",
        ["raw_video_id"],
        ["id"],
    )
    op.create_index("ix_assets_raw_video_id", "assets", ["raw_video_id"])


def downgrade() -> None:
    op.drop_index("ix_assets_raw_video_id", table_name="assets")
    op.drop_constraint("fk_assets_raw_video_id_raw_videos", "assets", type_="foreignkey")
    op.drop_column("assets", "raw_video_id")

    op.drop_index("ix_raw_videos_last_seen_at", table_name="raw_videos")
    op.drop_index("ix_raw_videos_status", table_name="raw_videos")
    op.drop_table("raw_videos")
