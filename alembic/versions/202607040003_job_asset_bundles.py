"""add job asset bundles

Revision ID: 202607040003
Revises: 202607040002
Create Date: 2026-07-04 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607040003"
down_revision: str | Sequence[str] | None = "202607040002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE job_asset_bundles DROP CONSTRAINT IF EXISTS job_asset_bundles_job_id_key"
    )
    op.execute(
        "ALTER TABLE job_asset_bundles DROP CONSTRAINT IF EXISTS ck_job_asset_bundles_status"
    )

    op.add_column(
        "job_asset_bundles",
        sa.Column("bundle_uid", sa.String(length=160), nullable=True),
    )
    op.add_column("job_asset_bundles", sa.Column("brand_id", sa.Integer(), nullable=True))
    op.add_column("job_asset_bundles", sa.Column("product_id", sa.Integer(), nullable=True))
    op.add_column("job_asset_bundles", sa.Column("niche_id", sa.Integer(), nullable=True))
    op.add_column("job_asset_bundles", sa.Column("request_json", sa.JSON(), nullable=True))
    op.add_column("job_asset_bundles", sa.Column("manifest_json", sa.JSON(), nullable=True))
    op.add_column(
        "job_asset_bundles",
        sa.Column("total_scenes", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("total_assets", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "job_asset_bundles",
        sa.Column("created_by", sa.String(length=160), nullable=True),
    )
    op.add_column("job_asset_bundles", sa.Column("error", sa.Text(), nullable=True))

    op.execute("UPDATE job_asset_bundles SET bundle_uid = 'legacy-' || id WHERE bundle_uid IS NULL")
    op.execute("UPDATE job_asset_bundles SET request_json = '{}'::json WHERE request_json IS NULL")
    op.execute(
        """
        UPDATE job_asset_bundles
        SET manifest_json = json_build_object(
            'bundle_uid', bundle_uid,
            'job_id', job_id,
            'brand_slug', '',
            'product_slug', NULL,
            'generated_at', created_at,
            'scenes', '[]'::json
        )
        WHERE manifest_json IS NULL
        """
    )

    op.alter_column(
        "job_asset_bundles",
        "bundle_uid",
        existing_type=sa.String(length=160),
        nullable=False,
    )
    op.alter_column("job_asset_bundles", "request_json", existing_type=sa.JSON(), nullable=False)
    op.alter_column("job_asset_bundles", "manifest_json", existing_type=sa.JSON(), nullable=False)
    op.alter_column(
        "job_asset_bundles",
        "output_dir",
        existing_type=sa.String(length=1200),
        nullable=True,
    )
    op.alter_column(
        "job_asset_bundles",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'ready'"),
    )

    op.create_check_constraint(
        "ck_job_asset_bundles_status",
        "job_asset_bundles",
        "status in ('ready', 'failed', 'partial', 'superseded', 'prepared', 'cleaned')",
    )
    op.create_unique_constraint(
        "uq_job_asset_bundles_bundle_uid",
        "job_asset_bundles",
        ["bundle_uid"],
    )
    op.create_index("ix_job_asset_bundles_job_id", "job_asset_bundles", ["job_id"], unique=False)
    op.create_index(
        "ix_job_asset_bundles_brand_id",
        "job_asset_bundles",
        ["brand_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_asset_bundles_product_id",
        "job_asset_bundles",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_asset_bundles_niche_id",
        "job_asset_bundles",
        ["niche_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_job_asset_bundles_brand_id_brands",
        "job_asset_bundles",
        "brands",
        ["brand_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_job_asset_bundles_product_id_products",
        "job_asset_bundles",
        "products",
        ["product_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_job_asset_bundles_niche_id_niches",
        "job_asset_bundles",
        "niches",
        ["niche_id"],
        ["id"],
    )

    op.create_table(
        "job_asset_bundle_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bundle_id", sa.Integer(), nullable=False),
        sa.Column("scene_id", sa.String(length=160), nullable=False),
        sa.Column("scene_index", sa.Integer(), nullable=True),
        sa.Column("asset_id", sa.Integer(), nullable=True),
        sa.Column("asset_uid", sa.String(length=160), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("match_reasons", sa.JSON(), nullable=True),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["bundle_id"], ["job_asset_bundles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_asset_bundle_items_bundle_id",
        "job_asset_bundle_items",
        ["bundle_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_asset_bundle_items_scene_id",
        "job_asset_bundle_items",
        ["scene_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_asset_bundle_items_asset_id",
        "job_asset_bundle_items",
        ["asset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_job_asset_bundle_items_asset_id", table_name="job_asset_bundle_items")
    op.drop_index("ix_job_asset_bundle_items_scene_id", table_name="job_asset_bundle_items")
    op.drop_index("ix_job_asset_bundle_items_bundle_id", table_name="job_asset_bundle_items")
    op.drop_table("job_asset_bundle_items")

    op.drop_constraint(
        "fk_job_asset_bundles_niche_id_niches",
        "job_asset_bundles",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_job_asset_bundles_product_id_products",
        "job_asset_bundles",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_job_asset_bundles_brand_id_brands",
        "job_asset_bundles",
        type_="foreignkey",
    )
    op.drop_index("ix_job_asset_bundles_niche_id", table_name="job_asset_bundles")
    op.drop_index("ix_job_asset_bundles_product_id", table_name="job_asset_bundles")
    op.drop_index("ix_job_asset_bundles_brand_id", table_name="job_asset_bundles")
    op.drop_index("ix_job_asset_bundles_job_id", table_name="job_asset_bundles")
    op.drop_constraint("uq_job_asset_bundles_bundle_uid", "job_asset_bundles", type_="unique")
    op.drop_constraint("ck_job_asset_bundles_status", "job_asset_bundles", type_="check")
    op.execute(
        """
        UPDATE job_asset_bundles
        SET status = 'prepared'
        WHERE status IN ('ready', 'partial', 'superseded')
        """
    )
    op.create_check_constraint(
        "ck_job_asset_bundles_status",
        "job_asset_bundles",
        "status in ('prepared', 'failed', 'cleaned')",
    )

    op.alter_column(
        "job_asset_bundles",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'prepared'"),
    )
    op.alter_column(
        "job_asset_bundles",
        "output_dir",
        existing_type=sa.String(length=1200),
        nullable=False,
    )
    op.drop_column("job_asset_bundles", "error")
    op.drop_column("job_asset_bundles", "created_by")
    op.drop_column("job_asset_bundles", "total_assets")
    op.drop_column("job_asset_bundles", "total_scenes")
    op.drop_column("job_asset_bundles", "manifest_json")
    op.drop_column("job_asset_bundles", "request_json")
    op.drop_column("job_asset_bundles", "niche_id")
    op.drop_column("job_asset_bundles", "product_id")
    op.drop_column("job_asset_bundles", "brand_id")
    op.drop_column("job_asset_bundles", "bundle_uid")
    op.create_unique_constraint("job_asset_bundles_job_id_key", "job_asset_bundles", ["job_id"])
