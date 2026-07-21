"""add source sync state

Revision ID: 202607210001
Revises: 202607050001
Create Date: 2026-07-21 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607210001"
down_revision: str | Sequence[str] | None = "202607050001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("folder_url", sa.String(length=1200), nullable=True))
    op.add_column("sources", sa.Column("provider_folder_id", sa.String(length=255), nullable=True))
    op.add_column(
        "sources",
        sa.Column("sync_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("sources", sa.Column("last_sync_status", sa.String(length=32), nullable=True))
    op.add_column("sources", sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("last_sync_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("last_sync_error", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("last_sync_summary", sa.JSON(), nullable=True))

    op.add_column("assets", sa.Column("remote_file_id", sa.String(length=255), nullable=True))
    op.add_column("assets", sa.Column("source_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("assets", sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("source_hash", sa.String(length=160), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "source_status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("source_last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("source_missing_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_assets_source_status",
        "assets",
        "source_status in ('active', 'missing', 'inaccessible', 'deleted', 'moved', 'changed')",
    )
    op.create_index("ix_assets_source_status", "assets", ["source_status"])
    op.create_index("ix_assets_source_id_remote_file_id", "assets", ["source_id", "remote_file_id"])

    op.create_table(
        "source_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("run_uid", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.CheckConstraint(
            "status in ('scanning', 'dry_run_ready', 'applied', 'failed', 'partial')",
            name="ck_source_sync_runs_status",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_uid"),
    )
    op.create_index("ix_source_sync_runs_source_id", "source_sync_runs", ["source_id"])
    op.create_index("ix_source_sync_runs_created_at", "source_sync_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_source_sync_runs_created_at", table_name="source_sync_runs")
    op.drop_index("ix_source_sync_runs_source_id", table_name="source_sync_runs")
    op.drop_table("source_sync_runs")

    op.drop_index("ix_assets_source_id_remote_file_id", table_name="assets")
    op.drop_index("ix_assets_source_status", table_name="assets")
    op.drop_constraint("ck_assets_source_status", "assets", type_="check")
    op.drop_column("assets", "source_missing_at")
    op.drop_column("assets", "source_last_seen_at")
    op.drop_column("assets", "source_status")
    op.drop_column("assets", "source_hash")
    op.drop_column("assets", "source_modified_at")
    op.drop_column("assets", "source_size_bytes")
    op.drop_column("assets", "remote_file_id")

    op.drop_column("sources", "last_sync_summary")
    op.drop_column("sources", "last_sync_error")
    op.drop_column("sources", "last_sync_completed_at")
    op.drop_column("sources", "last_sync_started_at")
    op.drop_column("sources", "last_sync_status")
    op.drop_column("sources", "sync_enabled")
    op.drop_column("sources", "provider_folder_id")
    op.drop_column("sources", "folder_url")
