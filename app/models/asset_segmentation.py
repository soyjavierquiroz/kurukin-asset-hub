from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class AssetSegmentationRun(Base):
    __tablename__ = "asset_segmentation_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('processing', 'ready', 'failed', 'partial')",
            name="ck_asset_segmentation_runs_status",
        ),
        Index("ix_asset_segmentation_runs_parent_asset_id", "parent_asset_id"),
        Index("ix_asset_segmentation_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    parent_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(160))

    parent_asset: Mapped["Asset"] = relationship(foreign_keys=[parent_asset_id])
    segments: Mapped[list["AssetSegment"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class AssetSegment(Base):
    __tablename__ = "asset_segments"
    __table_args__ = (
        CheckConstraint(
            "status in ('planned', 'created', 'uploaded', 'cataloged', 'failed', 'skipped')",
            name="ck_asset_segments_status",
        ),
        Index("ix_asset_segments_run_id", "run_id"),
        Index("ix_asset_segments_parent_asset_id", "parent_asset_id"),
        Index("ix_asset_segments_child_asset_id", "child_asset_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("asset_segmentation_runs.id"), nullable=False)
    parent_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    child_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str | None] = mapped_column(String(500))
    output_filename: Mapped[str | None] = mapped_column(String(500))
    remote_path: Mapped[str | None] = mapped_column(String(1200))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[AssetSegmentationRun] = relationship(back_populates="segments")
    parent_asset: Mapped["Asset"] = relationship(foreign_keys=[parent_asset_id])
    child_asset: Mapped["Asset | None"] = relationship(foreign_keys=[child_asset_id])
