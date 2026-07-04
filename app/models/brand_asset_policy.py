from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.brand import Brand

DEFAULT_ASSET_SCOPE_VALUES = (
    "global",
    "brand_exclusive",
    "allowed_brands",
    "collection_only",
    "restricted",
)


class BrandAssetPolicy(TimestampMixin, Base):
    __tablename__ = "brand_asset_policies"
    __table_args__ = (
        CheckConstraint(
            f"default_asset_scope in {DEFAULT_ASSET_SCOPE_VALUES}",
            name="ck_brand_asset_policies_default_asset_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), unique=True)
    allow_global_assets: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_global_video: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_global_image: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_stock_assets: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_stock_video: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_stock_image: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_stock_audio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    require_brand_match_for_video: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    require_brand_match_for_image: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    allow_global_audio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    default_asset_scope: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="brand_exclusive",
        server_default=text("'brand_exclusive'"),
    )

    brand: Mapped["Brand"] = relationship(back_populates="asset_policy")
