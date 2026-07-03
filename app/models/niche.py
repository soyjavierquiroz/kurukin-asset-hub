from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset


class Niche(TimestampMixin, Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    assets: Mapped[list["Asset"]] = relationship(
        secondary="asset_niches",
        back_populates="niches",
    )

