from typing import TYPE_CHECKING

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import PROVIDER_VALUES, TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.auth_profile import AuthProfile


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            f"provider in {PROVIDER_VALUES}",
            name="ck_sources_provider",
        ),
        Index("ix_sources_provider", "provider"),
        Index("ix_sources_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_profile_id: Mapped[int | None] = mapped_column(ForeignKey("auth_profiles.id"))
    rclone_remote: Mapped[str | None] = mapped_column(String(255))
    root_path: Mapped[str | None] = mapped_column(String(1000))
    folder_url: Mapped[str | None] = mapped_column(String(1200))
    provider_folder_id: Mapped[str | None] = mapped_column(String(255))
    sync_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    last_sync_status: Mapped[str | None] = mapped_column(String(32))
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    last_sync_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    auth_profile: Mapped["AuthProfile | None"] = relationship(back_populates="sources")
    assets: Mapped[list["Asset"]] = relationship(back_populates="source")
    sync_runs: Mapped[list["SourceSyncRun"]] = relationship(back_populates="source")


class SourceSyncRun(Base):
    __tablename__ = "source_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('scanning', 'dry_run_ready', 'applied', 'failed', 'partial')",
            name="ck_source_sync_runs_status",
        ),
        Index("ix_source_sync_runs_source_id", "source_id"),
        Index("ix_source_sync_runs_created_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    run_uid: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dry_run: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(160))

    source: Mapped[Source] = relationship(back_populates="sync_runs")
