from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AssetCollection(Base):
    __tablename__ = "asset_collections"

    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )

