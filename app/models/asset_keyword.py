from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class AssetKeyword(Base):
    __tablename__ = "asset_keywords"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "keyword",
            "category",
            "language",
            name="uq_asset_keywords_key",
        ),
        Index("ix_asset_keywords_asset_id", "asset_id"),
        Index("ix_asset_keywords_keyword", "keyword"),
        Index("ix_asset_keywords_category", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    keyword: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="und")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    asset: Mapped["Asset"] = relationship(back_populates="keywords")
