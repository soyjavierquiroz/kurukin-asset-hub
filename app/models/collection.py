from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.brand import Brand
    from app.models.product import Product


class Collection(TimestampMixin, Base):
    __tablename__ = "collections"
    __table_args__ = (
        Index("ix_collections_brand_id", "brand_id"),
        Index("ix_collections_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))

    brand: Mapped["Brand | None"] = relationship(back_populates="collections")
    product: Mapped["Product | None"] = relationship(back_populates="collections")
    assets: Mapped[list["Asset"]] = relationship(
        secondary="asset_collections",
        back_populates="collections",
    )

