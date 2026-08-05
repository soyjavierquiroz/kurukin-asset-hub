"""Add compact_v2 path layout metadata.

Revision ID: 202608040002
Revises: 202608040001
Create Date: 2026-08-04 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "202608040002"
down_revision = "202608040001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("path_layout_version", sa.String(length=80), nullable=True))
    op.add_column("managed_drive_folders", sa.Column("path_layout_version", sa.String(length=80), nullable=True))
    op.add_column("managed_drive_folders", sa.Column("context_slug", sa.String(length=180), nullable=True))


def downgrade() -> None:
    op.drop_column("managed_drive_folders", "context_slug")
    op.drop_column("managed_drive_folders", "path_layout_version")
    op.drop_column("assets", "path_layout_version")
