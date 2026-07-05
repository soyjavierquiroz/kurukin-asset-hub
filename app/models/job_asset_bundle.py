from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    BigInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.brand import Brand
    from app.models.niche import Niche
    from app.models.product import Product

JOB_ASSET_BUNDLE_STATUS_VALUES = (
    "ready",
    "failed",
    "partial",
    "superseded",
    "prepared",
    "cleaned",
)
JOB_ASSET_BUNDLE_MATERIALIZATION_STATUS_VALUES = (
    "pending",
    "processing",
    "ready",
    "failed",
    "partial",
    "skipped",
)
JOB_ASSET_BUNDLE_ITEM_MATERIALIZATION_STATUS_VALUES = (
    "pending",
    "processing",
    "ready",
    "failed",
    "skipped",
)


class JobAssetBundle(TimestampMixin, Base):
    __tablename__ = "job_asset_bundles"
    __table_args__ = (
        CheckConstraint(
            f"status in {JOB_ASSET_BUNDLE_STATUS_VALUES}",
            name="ck_job_asset_bundles_status",
        ),
        CheckConstraint(
            f"materialization_status in {JOB_ASSET_BUNDLE_MATERIALIZATION_STATUS_VALUES}",
            name="ck_job_asset_bundles_materialization_status",
        ),
        Index("ix_job_asset_bundles_job_id", "job_id"),
        Index("ix_job_asset_bundles_brand_id", "brand_id"),
        Index("ix_job_asset_bundles_product_id", "product_id"),
        Index("ix_job_asset_bundles_niche_id", "niche_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_uid: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ready",
        server_default=text("'ready'"),
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    total_scenes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    total_assets: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_by: Mapped[str | None] = mapped_column(String(160))
    error: Mapped[str | None] = mapped_column(Text)
    output_dir: Mapped[str | None] = mapped_column(String(1200))
    manifest_path: Mapped[str | None] = mapped_column(String(1200))
    materialization_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    materialization_error: Mapped[str | None] = mapped_column(Text)
    materialized_assets_dir: Mapped[str | None] = mapped_column(String(1200))
    renderer_manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    brand: Mapped["Brand | None"] = relationship()
    product: Mapped["Product | None"] = relationship()
    niche: Mapped["Niche | None"] = relationship()
    items: Mapped[list["JobAssetBundleItem"]] = relationship(
        back_populates="bundle",
        cascade="all, delete-orphan",
        order_by="JobAssetBundleItem.id",
    )


class JobAssetBundleItem(Base):
    __tablename__ = "job_asset_bundle_items"
    __table_args__ = (
        CheckConstraint(
            f"materialization_status in {JOB_ASSET_BUNDLE_ITEM_MATERIALIZATION_STATUS_VALUES}",
            name="ck_job_asset_bundle_items_materialization_status",
        ),
        Index("ix_job_asset_bundle_items_bundle_id", "bundle_id"),
        Index("ix_job_asset_bundle_items_scene_id", "scene_id"),
        Index("ix_job_asset_bundle_items_asset_id", "asset_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_id: Mapped[int] = mapped_column(
        ForeignKey("job_asset_bundles.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_id: Mapped[str] = mapped_column(String(160), nullable=False)
    scene_index: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    asset_uid: Mapped[str | None] = mapped_column(String(160))
    score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    match_reasons: Mapped[list[str] | None] = mapped_column(JSON)
    selection_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    materialization_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    local_path: Mapped[str | None] = mapped_column(String(1200))
    relative_path: Mapped[str | None] = mapped_column(String(1200))
    materialized_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    materialized_sha256: Mapped[str | None] = mapped_column(String(128))
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    materialization_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    bundle: Mapped["JobAssetBundle"] = relationship(back_populates="items")
    asset: Mapped["Asset | None"] = relationship()
