"""add job bundle materialization state

Revision ID: 202607050001
Revises: 202607040003
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607050001"
down_revision: str | Sequence[str] | None = "202607040003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_asset_bundles",
        sa.Column(
            "materialization_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("materialization_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("materialized_assets_dir", sa.String(length=1200), nullable=True),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("renderer_manifest_json", sa.JSON(), nullable=True),
    )
    op.create_check_constraint(
        "ck_job_asset_bundles_materialization_status",
        "job_asset_bundles",
        "materialization_status in "
        "('pending', 'processing', 'ready', 'failed', 'partial', 'skipped')",
    )

    op.add_column(
        "job_asset_bundle_items",
        sa.Column(
            "materialization_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("local_path", sa.String(length=1200), nullable=True),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("relative_path", sa.String(length=1200), nullable=True),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("materialized_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("materialized_sha256", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_asset_bundle_items",
        sa.Column("materialization_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_job_asset_bundle_items_materialization_status",
        "job_asset_bundle_items",
        "materialization_status in ('pending', 'processing', 'ready', 'failed', 'skipped')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_job_asset_bundle_items_materialization_status",
        "job_asset_bundle_items",
        type_="check",
    )
    op.drop_column("job_asset_bundle_items", "materialization_error")
    op.drop_column("job_asset_bundle_items", "materialized_at")
    op.drop_column("job_asset_bundle_items", "materialized_sha256")
    op.drop_column("job_asset_bundle_items", "materialized_size_bytes")
    op.drop_column("job_asset_bundle_items", "relative_path")
    op.drop_column("job_asset_bundle_items", "local_path")
    op.drop_column("job_asset_bundle_items", "materialization_status")

    op.drop_constraint(
        "ck_job_asset_bundles_materialization_status",
        "job_asset_bundles",
        type_="check",
    )
    op.drop_column("job_asset_bundles", "renderer_manifest_json")
    op.drop_column("job_asset_bundles", "materialized_assets_dir")
    op.drop_column("job_asset_bundles", "materialization_error")
    op.drop_column("job_asset_bundles", "materialized_at")
    op.drop_column("job_asset_bundles", "materialization_status")
