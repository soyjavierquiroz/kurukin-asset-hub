"""add ai asset enrichment

Revision ID: 202607040002
Revises: 202607040001
Create Date: 2026-07-04 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "202607040002"
down_revision: str | Sequence[str] | None = "202607040001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column(
            "ai_enrichment_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("ai_enrichment_confidence", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("ai_enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("ai_model", sa.String(length=160), nullable=True))
    op.add_column("assets", sa.Column("ai_error", sa.Text(), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "needs_human_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("review_reason", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("reviewed_by", sa.String(length=160), nullable=True))
    op.add_column("assets", sa.Column("enrichment_version", sa.String(length=80), nullable=True))
    op.create_check_constraint(
        "ck_assets_ai_enrichment_status",
        "assets",
        (
            "ai_enrichment_status in "
            "('pending', 'processing', 'ready', 'failed', 'needs_review', 'skipped')"
        ),
    )

    op.create_table(
        "asset_ai_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=80),
            server_default=sa.text("'openai'"),
            nullable=False,
        ),
        sa.Column("input_type", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
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
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_ai_analyses_asset_id", "asset_ai_analyses", ["asset_id"], unique=False)
    op.create_index("ix_asset_ai_analyses_provider", "asset_ai_analyses", ["provider"], unique=False)
    op.create_index("ix_asset_ai_analyses_model", "asset_ai_analyses", ["model"], unique=False)
    op.create_index(
        "ix_asset_ai_analyses_prompt_version",
        "asset_ai_analyses",
        ["prompt_version"],
        unique=False,
    )
    op.create_index(
        "ix_asset_ai_analyses_created_at",
        "asset_ai_analyses",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_asset_ai_analyses_created_at", table_name="asset_ai_analyses")
    op.drop_index("ix_asset_ai_analyses_prompt_version", table_name="asset_ai_analyses")
    op.drop_index("ix_asset_ai_analyses_model", table_name="asset_ai_analyses")
    op.drop_index("ix_asset_ai_analyses_provider", table_name="asset_ai_analyses")
    op.drop_index("ix_asset_ai_analyses_asset_id", table_name="asset_ai_analyses")
    op.drop_table("asset_ai_analyses")
    op.drop_constraint("ck_assets_ai_enrichment_status", "assets", type_="check")
    op.drop_column("assets", "enrichment_version")
    op.drop_column("assets", "reviewed_by")
    op.drop_column("assets", "reviewed_at")
    op.drop_column("assets", "review_reason")
    op.drop_column("assets", "needs_human_review")
    op.drop_column("assets", "ai_error")
    op.drop_column("assets", "ai_model")
    op.drop_column("assets", "ai_enriched_at")
    op.drop_column("assets", "ai_enrichment_confidence")
    op.drop_column("assets", "ai_enrichment_status")
