from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset

RAW_VIDEO_STATUS_VALUES = ("NEW", "PROCESSING", "DONE", "FAILED")


class RawVideo(TimestampMixin, Base):
    __tablename__ = "raw_videos"
    __table_args__ = (
        CheckConstraint(
            f"status in {RAW_VIDEO_STATUS_VALUES}",
            name="ck_raw_videos_status",
        ),
        Index("ix_raw_videos_status", "status"),
        Index("ix_raw_videos_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="gdrive_people_raw_long",
        server_default=text("'gdrive_people_raw_long'"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    segments_generated: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    assets: Mapped[list["Asset"]] = relationship(back_populates="raw_video")
