from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class AssetUsage(Base):
    __tablename__ = "asset_usages"
    __table_args__ = (
        Index("ix_asset_usages_asset_id", "asset_id"),
        Index("ix_asset_usages_job_id", "job_id"),
        Index("ix_asset_usages_used_at", "used_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    renderer: Mapped[str | None] = mapped_column(String(160))
    local_path: Mapped[str | None] = mapped_column(String(1200))
    manifest_path: Mapped[str | None] = mapped_column(String(1200))
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    notes: Mapped[str | None] = mapped_column(Text)

    asset: Mapped["Asset"] = relationship(back_populates="usages")

