from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Index, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.collection import Collection
    from app.models.product import Product


class Brand(TimestampMixin, Base):
    __tablename__ = "brands"
    __table_args__ = (
        Index("ix_brands_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visual_line: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    products: Mapped[list["Product"]] = relationship(back_populates="brand")
    collections: Mapped[list["Collection"]] = relationship(back_populates="brand")
    assets: Mapped[list["Asset"]] = relationship(back_populates="brand")

