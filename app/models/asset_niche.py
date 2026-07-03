from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AssetNiche(Base):
    __tablename__ = "asset_niches"

    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    niche_id: Mapped[int] = mapped_column(
        ForeignKey("niches.id", ondelete="CASCADE"),
        primary_key=True,
    )

