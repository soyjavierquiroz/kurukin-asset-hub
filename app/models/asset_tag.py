from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class AssetTag(Base):
    __tablename__ = "asset_tags"
    __table_args__ = (
        UniqueConstraint("asset_id", "tag", name="uq_asset_tags_asset_id_tag"),
        Index("ix_asset_tags_asset_id", "asset_id"),
        Index("ix_asset_tags_tag", "tag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    asset: Mapped["Asset"] = relationship(back_populates="tags")

