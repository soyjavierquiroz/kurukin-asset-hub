from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.brand import Brand
    from app.models.collection import Collection
    from app.models.product_asset_policy import ProductAssetPolicy


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("brand_id", "slug", name="uq_products_brand_id_slug"),
        Index("ix_products_brand_id", "brand_id"),
        Index("ix_products_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    brand: Mapped["Brand"] = relationship(back_populates="products")
    collections: Mapped[list["Collection"]] = relationship(back_populates="product")
    assets: Mapped[list["Asset"]] = relationship(back_populates="product")
    asset_policy: Mapped["ProductAssetPolicy | None"] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        uselist=False,
    )
