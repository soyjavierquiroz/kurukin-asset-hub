from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.product import Product

PRODUCT_USAGE_SCOPE_VALUES = (
    "global",
    "brand_exclusive",
    "allowed_brands",
    "collection_only",
    "restricted",
)


class ProductAssetPolicy(TimestampMixin, Base):
    __tablename__ = "product_asset_policies"
    __table_args__ = (
        CheckConstraint(
            f"default_usage_scope in {PRODUCT_USAGE_SCOPE_VALUES}",
            name="ck_product_asset_policies_default_usage_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    inherit_brand_policy: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    default_usage_scope: Mapped[str | None] = mapped_column(String(40))
    allow_global_video: Mapped[bool | None] = mapped_column(Boolean)
    allow_global_image: Mapped[bool | None] = mapped_column(Boolean)
    allow_global_audio: Mapped[bool | None] = mapped_column(Boolean)
    allow_stock_video: Mapped[bool | None] = mapped_column(Boolean)
    allow_stock_image: Mapped[bool | None] = mapped_column(Boolean)
    allow_stock_audio: Mapped[bool | None] = mapped_column(Boolean)
    require_product_match_for_video: Mapped[bool | None] = mapped_column(Boolean)
    require_product_match_for_image: Mapped[bool | None] = mapped_column(Boolean)
    auto_select_enabled_default: Mapped[bool | None] = mapped_column(Boolean)

    product: Mapped["Product"] = relationship(back_populates="asset_policy")
