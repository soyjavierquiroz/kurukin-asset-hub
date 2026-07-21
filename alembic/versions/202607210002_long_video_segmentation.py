"""add long video segmentation

Revision ID: 202607210002
Revises: 202607210001
Create Date: 2026-07-21 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607210002"
down_revision: str | Sequence[str] | None = "202607210001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("source_role", sa.String(length=40), nullable=True))
    op.add_column("sources", sa.Column("derived_rclone_remote", sa.String(length=255), nullable=True))
    op.add_column("sources", sa.Column("derived_root_path", sa.String(length=1000), nullable=True))
    op.add_column("sources", sa.Column("derived_source_id", sa.String(length=120), nullable=True))

    op.add_column("assets", sa.Column("parent_asset_id", sa.Integer(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("is_derivative", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("assets", sa.Column("derivative_type", sa.String(length=80), nullable=True))
    op.add_column("assets", sa.Column("segment_index", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("segment_start_seconds", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("segment_end_seconds", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("segmentation_status", sa.String(length=32), nullable=True))
    op.add_column("assets", sa.Column("segmented_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("segment_count", sa.Integer(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("delete_original_eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("assets", sa.Column("original_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("original_delete_status", sa.String(length=32), nullable=True))
    op.add_column("assets", sa.Column("original_delete_error", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("segmentation_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("segmentation_approved_by", sa.String(length=160), nullable=True))
    op.create_foreign_key("fk_assets_parent_asset_id_assets", "assets", "assets", ["parent_asset_id"], ["id"])
    op.create_check_constraint(
        "ck_assets_segmentation_status",
        "assets",
        "segmentation_status in ('pending', 'processing', 'ready', 'failed', 'partial', 'skipped')",
    )
    op.create_check_constraint(
        "ck_assets_original_delete_status",
        "assets",
        "original_delete_status in ('pending', 'deleted', 'failed', 'skipped')",
    )
    op.create_index("ix_assets_parent_asset_id", "assets", ["parent_asset_id"])
    op.create_index("ix_assets_is_derivative", "assets", ["is_derivative"])
    op.create_index("ix_assets_segmentation_status", "assets", ["segmentation_status"])

    op.create_table(
        "asset_segmentation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_uid", sa.String(length=160), nullable=False),
        sa.Column("parent_asset_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.CheckConstraint(
            "status in ('processing', 'ready', 'failed', 'partial')",
            name="ck_asset_segmentation_runs_status",
        ),
        sa.ForeignKeyConstraint(["parent_asset_id"], ["assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_uid"),
    )
    op.create_index(
        "ix_asset_segmentation_runs_parent_asset_id",
        "asset_segmentation_runs",
        ["parent_asset_id"],
    )
    op.create_index(
        "ix_asset_segmentation_runs_started_at",
        "asset_segmentation_runs",
        ["started_at"],
    )

    op.create_table(
        "asset_segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("parent_asset_id", sa.Integer(), nullable=False),
        sa.Column("child_asset_id", sa.Integer(), nullable=True),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("output_filename", sa.String(length=500), nullable=True),
        sa.Column("remote_path", sa.String(length=1200), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('planned', 'created', 'uploaded', 'cataloged', 'failed', 'skipped')",
            name="ck_asset_segments_status",
        ),
        sa.ForeignKeyConstraint(["child_asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["parent_asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["asset_segmentation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_segments_run_id", "asset_segments", ["run_id"])
    op.create_index("ix_asset_segments_parent_asset_id", "asset_segments", ["parent_asset_id"])
    op.create_index("ix_asset_segments_child_asset_id", "asset_segments", ["child_asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_segments_child_asset_id", table_name="asset_segments")
    op.drop_index("ix_asset_segments_parent_asset_id", table_name="asset_segments")
    op.drop_index("ix_asset_segments_run_id", table_name="asset_segments")
    op.drop_table("asset_segments")
    op.drop_index("ix_asset_segmentation_runs_started_at", table_name="asset_segmentation_runs")
    op.drop_index("ix_asset_segmentation_runs_parent_asset_id", table_name="asset_segmentation_runs")
    op.drop_table("asset_segmentation_runs")

    op.drop_index("ix_assets_segmentation_status", table_name="assets")
    op.drop_index("ix_assets_is_derivative", table_name="assets")
    op.drop_index("ix_assets_parent_asset_id", table_name="assets")
    op.drop_constraint("ck_assets_original_delete_status", "assets", type_="check")
    op.drop_constraint("ck_assets_segmentation_status", "assets", type_="check")
    op.drop_constraint("fk_assets_parent_asset_id_assets", "assets", type_="foreignkey")
    op.drop_column("assets", "segmentation_approved_by")
    op.drop_column("assets", "segmentation_approved_at")
    op.drop_column("assets", "original_delete_error")
    op.drop_column("assets", "original_delete_status")
    op.drop_column("assets", "original_deleted_at")
    op.drop_column("assets", "delete_original_eligible")
    op.drop_column("assets", "segment_count")
    op.drop_column("assets", "segmented_at")
    op.drop_column("assets", "segmentation_status")
    op.drop_column("assets", "segment_end_seconds")
    op.drop_column("assets", "segment_start_seconds")
    op.drop_column("assets", "segment_index")
    op.drop_column("assets", "derivative_type")
    op.drop_column("assets", "is_derivative")
    op.drop_column("assets", "parent_asset_id")

    op.drop_column("sources", "derived_source_id")
    op.drop_column("sources", "derived_root_path")
    op.drop_column("sources", "derived_rclone_remote")
    op.drop_column("sources", "source_role")
