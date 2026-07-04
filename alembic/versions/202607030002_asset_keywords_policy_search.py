"""add asset keywords and policy search fields

Revision ID: 202607030002
Revises: eee1d487fbcd
Create Date: 2026-07-03 21:10:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607030002"
down_revision: str | Sequence[str] | None = "eee1d487fbcd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("search_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("embedding_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("negative_keywords", sa.Text(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("auto_keywords_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assets",
        sa.Column(
            "usage_scope",
            sa.String(length=40),
            server_default=sa.text("'global'"),
            nullable=False,
        ),
    )
    op.add_column(
        "assets",
        sa.Column(
            "rights_status",
            sa.String(length=40),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
    )
    op.add_column(
        "assets",
        sa.Column(
            "auto_select_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_assets_usage_scope",
        "assets",
        (
            "usage_scope in ('global', 'brand_exclusive', 'allowed_brands', "
            "'collection_only', 'restricted')"
        ),
    )
    op.create_check_constraint(
        "ck_assets_rights_status",
        "assets",
        "rights_status in ('owned', 'licensed', 'stock', 'unknown', 'restricted')",
    )
    op.create_index("ix_assets_usage_scope", "assets", ["usage_scope"], unique=False)
    op.create_index(
        "ix_assets_auto_select_enabled",
        "assets",
        ["auto_select_enabled"],
        unique=False,
    )

    op.create_table(
        "asset_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(length=160), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asset_id",
            "keyword",
            "category",
            "language",
            name="uq_asset_keywords_key",
        ),
    )
    op.create_index("ix_asset_keywords_asset_id", "asset_keywords", ["asset_id"], unique=False)
    op.create_index("ix_asset_keywords_keyword", "asset_keywords", ["keyword"], unique=False)
    op.create_index("ix_asset_keywords_category", "asset_keywords", ["category"], unique=False)

    op.create_table(
        "asset_allowed_brands",
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id", "brand_id"),
    )
    op.create_index(
        "ix_asset_allowed_brands_asset_id",
        "asset_allowed_brands",
        ["asset_id"],
        unique=False,
    )
    op.create_index(
        "ix_asset_allowed_brands_brand_id",
        "asset_allowed_brands",
        ["brand_id"],
        unique=False,
    )

    op.create_table(
        "brand_asset_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column(
            "allow_global_assets",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_global_video",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_global_image",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_stock_assets",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_stock_video",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_stock_image",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_stock_audio",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "require_brand_match_for_video",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "require_brand_match_for_image",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_global_audio",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "default_asset_scope",
            sa.String(length=40),
            server_default=sa.text("'brand_exclusive'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "default_asset_scope in ('global', 'brand_exclusive', 'allowed_brands', "
                "'collection_only', 'restricted')"
            ),
            name="ck_brand_asset_policies_default_asset_scope",
        ),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand_id"),
    )

    op.create_table(
        "product_asset_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "inherit_brand_policy",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("default_usage_scope", sa.String(length=40), nullable=True),
        sa.Column("allow_global_video", sa.Boolean(), nullable=True),
        sa.Column("allow_global_image", sa.Boolean(), nullable=True),
        sa.Column("allow_global_audio", sa.Boolean(), nullable=True),
        sa.Column("allow_stock_video", sa.Boolean(), nullable=True),
        sa.Column("allow_stock_image", sa.Boolean(), nullable=True),
        sa.Column("allow_stock_audio", sa.Boolean(), nullable=True),
        sa.Column("require_product_match_for_video", sa.Boolean(), nullable=True),
        sa.Column("require_product_match_for_image", sa.Boolean(), nullable=True),
        sa.Column("auto_select_enabled_default", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "default_usage_scope in ('global', 'brand_exclusive', 'allowed_brands', "
                "'collection_only', 'restricted')"
            ),
            name="ck_product_asset_policies_default_usage_scope",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id"),
    )


def downgrade() -> None:
    op.drop_table("product_asset_policies")
    op.drop_table("brand_asset_policies")
    op.drop_index("ix_asset_allowed_brands_brand_id", table_name="asset_allowed_brands")
    op.drop_index("ix_asset_allowed_brands_asset_id", table_name="asset_allowed_brands")
    op.drop_table("asset_allowed_brands")
    op.drop_index("ix_asset_keywords_category", table_name="asset_keywords")
    op.drop_index("ix_asset_keywords_keyword", table_name="asset_keywords")
    op.drop_index("ix_asset_keywords_asset_id", table_name="asset_keywords")
    op.drop_table("asset_keywords")
    op.drop_index("ix_assets_auto_select_enabled", table_name="assets")
    op.drop_index("ix_assets_usage_scope", table_name="assets")
    op.drop_constraint("ck_assets_rights_status", "assets", type_="check")
    op.drop_constraint("ck_assets_usage_scope", "assets", type_="check")
    op.drop_column("assets", "auto_select_enabled")
    op.drop_column("assets", "rights_status")
    op.drop_column("assets", "usage_scope")
    op.drop_column("assets", "auto_keywords_generated_at")
    op.drop_column("assets", "negative_keywords")
    op.drop_column("assets", "embedding_text")
    op.drop_column("assets", "search_text")
