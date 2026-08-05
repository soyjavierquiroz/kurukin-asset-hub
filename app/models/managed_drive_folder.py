from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.asset import ASSET_SCOPE_VALUES, TITLE_TYPE_VALUES
from app.models.common import TimestampMixin


class ManagedDriveFolder(TimestampMixin, Base):
    __tablename__ = "managed_drive_folders"
    __table_args__ = (
        CheckConstraint(f"scope in {ASSET_SCOPE_VALUES}", name="ck_managed_drive_folders_scope"),
        CheckConstraint(
            f"title_type is null or title_type in {TITLE_TYPE_VALUES}",
            name="ck_managed_drive_folders_title_type",
        ),
        UniqueConstraint(
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
        Index("ix_managed_drive_folders_drive_folder_id", "drive_folder_id"),
        Index("ix_managed_drive_folders_scope", "scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_folder_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    root_folder_id: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))
    title_slug: Mapped[str | None] = mapped_column(String(180))
    title_type: Mapped[str | None] = mapped_column(String(32))
    collection: Mapped[str | None] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    path_layout_version: Mapped[str | None] = mapped_column(String(80))
    context_slug: Mapped[str | None] = mapped_column(String(180))
    primary_theme: Mapped[str | None] = mapped_column(String(120))
    primary_topic: Mapped[str] = mapped_column(String(160), nullable=False)
    orientation: Mapped[str] = mapped_column(String(32), nullable=False)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
