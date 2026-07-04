from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.brand import Brand


class AssetAllowedBrand(Base):
    __tablename__ = "asset_allowed_brands"
    __table_args__ = (
        Index("ix_asset_allowed_brands_asset_id", "asset_id"),
        Index("ix_asset_allowed_brands_brand_id", "brand_id"),
    )

    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"),
        primary_key=True,
    )

    asset: Mapped["Asset"] = relationship(back_populates="allowed_brands")
    brand: Mapped["Brand"] = relationship(back_populates="allowed_assets")
