"""managed drive pilot

Revision ID: 202608030001
Revises: 202607220002
Create Date: 2026-08-03 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202608030001"
down_revision: str | Sequence[str] | None = "202607220002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ASSET_STATUS_VALUES = (
    "'active'",
    "'inactive'",
    "'missing'",
    "'archived'",
    "'discovered'",
    "'downloading'",
    "'technical_analysis'",
    "'preview_generation'",
    "'ai_analysis'",
    "'move_planned'",
    "'moving'",
    "'moved'",
    "'ready'",
    "'review_required'",
    "'failed'",
)
ORIENTATION_VALUES = (
    "'9:16'",
    "'16:9'",
    "'square'",
    "'unknown'",
    "'horizontal-16x9'",
    "'vertical-9x16'",
    "'vertical-4x5'",
    "'cuadrado-1x1'",
    "'panoramico'",
    "'otro'",
)
SCOPE_VALUES = ("'generic'", "'brand'", "'title'")
TITLE_TYPE_VALUES = ("'movie'", "'series'")
COLLECTION_VALUES = ("'evergreen'", "'campaign'", "'ugc'", "'brand-kit'")
MOVE_STATUS_VALUES = (
    "'not_planned'",
    "'planned'",
    "'moving'",
    "'moved'",
    "'verification_required'",
    "'rollback_required'",
    "'move_failed'",
)


def upgrade() -> None:
    op.drop_constraint("ck_assets_status", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_status",
        "assets",
        f"status in ({', '.join(ASSET_STATUS_VALUES)})",
    )
    op.drop_constraint("ck_assets_orientation", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_orientation",
        "assets",
        f"orientation in ({', '.join(ORIENTATION_VALUES)})",
    )

    op.add_column("assets", sa.Column("scope", sa.String(length=32), nullable=True))
    op.add_column("assets", sa.Column("title_type", sa.String(length=32), nullable=True))
    op.add_column("assets", sa.Column("title_name", sa.String(length=500), nullable=True))
    op.add_column("assets", sa.Column("title_slug", sa.String(length=180), nullable=True))
    op.add_column("assets", sa.Column("season_number", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("episode_number", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("collection", sa.String(length=32), nullable=True))
    op.add_column(
        "assets",
        sa.Column("generic_compatibility", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("assets", sa.Column("usage_policy", sa.JSON(), nullable=True))
    op.add_column("assets", sa.Column("original_parent_id", sa.String(length=255), nullable=True))
    op.add_column("assets", sa.Column("original_name", sa.String(length=500), nullable=True))
    op.add_column("assets", sa.Column("target_parent_id", sa.String(length=255), nullable=True))
    op.add_column("assets", sa.Column("target_name", sa.String(length=500), nullable=True))
    op.add_column("assets", sa.Column("path_layout_version", sa.String(length=80), nullable=True))
    op.add_column("assets", sa.Column("plan_version", sa.String(length=80), nullable=True))
    op.add_column("assets", sa.Column("plan_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("plan_hash", sa.String(length=80), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "move_status",
            sa.String(length=32),
            server_default=sa.text("'not_planned'"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("moved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("catalog_version", sa.String(length=80), nullable=True))
    op.add_column("assets", sa.Column("primary_theme", sa.String(length=120), nullable=True))
    op.add_column("assets", sa.Column("primary_topic", sa.String(length=160), nullable=True))
    op.add_column("assets", sa.Column("suggested_uses", sa.JSON(), nullable=True))
    op.add_column("assets", sa.Column("flip_risk_reasons", sa.JSON(), nullable=True))
    op.add_column("assets", sa.Column("max_safe_zoom", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("safe_text_areas", sa.JSON(), nullable=True))
    op.create_check_constraint(
        "ck_assets_scope",
        "assets",
        f"scope is null or scope in ({', '.join(SCOPE_VALUES)})",
    )
    op.create_check_constraint(
        "ck_assets_title_type",
        "assets",
        f"title_type is null or title_type in ({', '.join(TITLE_TYPE_VALUES)})",
    )
    op.create_check_constraint(
        "ck_assets_collection",
        "assets",
        f"collection is null or collection in ({', '.join(COLLECTION_VALUES)})",
    )
    op.create_check_constraint(
        "ck_assets_move_status",
        "assets",
        f"move_status in ({', '.join(MOVE_STATUS_VALUES)})",
    )
    op.create_check_constraint(
        "ck_assets_scope_owner",
        "assets",
        """
        scope is null
        or (
            (scope = 'generic' and brand_id is null and title_name is null)
            or (scope = 'brand' and brand_id is not null and title_name is null)
            or (scope = 'title' and title_name is not null and title_type is not null)
        )
        """,
    )
    op.create_index("ix_assets_scope", "assets", ["scope"])
    op.create_index("ix_assets_title_slug", "assets", ["title_slug"])
    op.create_index("ix_assets_move_status", "assets", ["move_status"])
    op.create_index("ix_assets_drive_file_id", "assets", ["drive_file_id"])

    op.create_table(
        "managed_drive_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drive_folder_id", sa.String(length=255), nullable=False),
        sa.Column("root_folder_id", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=True),
        sa.Column("title_slug", sa.String(length=180), nullable=True),
        sa.Column("title_type", sa.String(length=32), nullable=True),
        sa.Column("collection", sa.String(length=32), nullable=True),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("path_layout_version", sa.String(length=80), nullable=True),
        sa.Column("context_slug", sa.String(length=180), nullable=True),
        sa.Column("primary_theme", sa.String(length=120), nullable=True),
        sa.Column("primary_topic", sa.String(length=160), nullable=False),
        sa.Column("orientation", sa.String(length=32), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"scope in ({', '.join(SCOPE_VALUES)})", name="ck_managed_drive_folders_scope"),
        sa.CheckConstraint(
            f"title_type is null or title_type in ({', '.join(TITLE_TYPE_VALUES)})",
            name="ck_managed_drive_folders_title_type",
        ),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("drive_folder_id"),
        sa.UniqueConstraint(
            "root_folder_id",
            "scope",
            "brand_id",
            "title_slug",
            "title_type",
            "collection",
            "media_type",
            "path_layout_version",
            "context_slug",
            "primary_theme",
            "primary_topic",
            "orientation",
            "batch_number",
            name="uq_managed_drive_folder_classification",
        ),
    )
    op.create_index(
        "ix_managed_drive_folders_drive_folder_id",
        "managed_drive_folders",
        ["drive_folder_id"],
    )
    op.create_index("ix_managed_drive_folders_scope", "managed_drive_folders", ["scope"])


def downgrade() -> None:
    op.drop_index("ix_managed_drive_folders_scope", table_name="managed_drive_folders")
    op.drop_index("ix_managed_drive_folders_drive_folder_id", table_name="managed_drive_folders")
    op.drop_table("managed_drive_folders")
    op.drop_index("ix_assets_drive_file_id", table_name="assets")
    op.drop_index("ix_assets_move_status", table_name="assets")
    op.drop_index("ix_assets_title_slug", table_name="assets")
    op.drop_index("ix_assets_scope", table_name="assets")
    op.drop_constraint("ck_assets_scope_owner", "assets", type_="check")
    op.drop_constraint("ck_assets_move_status", "assets", type_="check")
    op.drop_constraint("ck_assets_collection", "assets", type_="check")
    op.drop_constraint("ck_assets_title_type", "assets", type_="check")
    op.drop_constraint("ck_assets_scope", "assets", type_="check")
    for column in (
        "safe_text_areas",
        "max_safe_zoom",
        "flip_risk_reasons",
        "suggested_uses",
        "primary_topic",
        "primary_theme",
        "catalog_version",
        "moved_at",
        "move_status",
        "target_name",
        "path_layout_version",
        "target_parent_id",
        "plan_hash",
        "plan_created_at",
        "plan_version",
        "original_name",
        "original_parent_id",
        "usage_policy",
        "generic_compatibility",
        "collection",
        "episode_number",
        "season_number",
        "title_slug",
        "title_name",
        "title_type",
        "scope",
    ):
        op.drop_column("assets", column)
    op.drop_constraint("ck_assets_orientation", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_orientation",
        "assets",
        "orientation in ('9:16', '16:9', 'square', 'unknown')",
    )
    op.drop_constraint("ck_assets_status", "assets", type_="check")
    op.create_check_constraint(
        "ck_assets_status",
        "assets",
        "status in ('active', 'inactive', 'missing', 'archived')",
    )
