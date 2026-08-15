"""Add editorial quality gate fields.

Revision ID: 202608150001
Revises: 202608040002
Create Date: 2026-08-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "202608150001"
down_revision = "202608040002"
branch_labels = None
depends_on = None


EDITORIAL_STATUS_VALUES = ("pending", "searchable", "quarantined", "rejected")


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column(
            "editorial_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column("assets", sa.Column("editorial_quality_score", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("vertical_suitability_score", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("horizontal_suitability_score", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("editorial_reason_codes", sa.JSON(), nullable=True))
    op.add_column("assets", sa.Column("quality_profile_version", sa.String(length=80), nullable=True))
    op.add_column("assets", sa.Column("quality_analyzed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_assets_editorial_status",
        "assets",
        f"editorial_status in {EDITORIAL_STATUS_VALUES}",
    )
    op.create_index("ix_assets_editorial_status", "assets", ["editorial_status"])
    op.create_index("ix_assets_quality_profile_version", "assets", ["quality_profile_version"])


def downgrade() -> None:
    op.drop_index("ix_assets_quality_profile_version", table_name="assets")
    op.drop_index("ix_assets_editorial_status", table_name="assets")
    op.drop_constraint("ck_assets_editorial_status", "assets", type_="check")
    op.drop_column("assets", "quality_analyzed_at")
    op.drop_column("assets", "quality_profile_version")
    op.drop_column("assets", "editorial_reason_codes")
    op.drop_column("assets", "horizontal_suitability_score")
    op.drop_column("assets", "vertical_suitability_score")
    op.drop_column("assets", "editorial_quality_score")
    op.drop_column("assets", "editorial_status")
