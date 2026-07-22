from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import PROVIDER_VALUES, TimestampMixin

if TYPE_CHECKING:
    from app.models.asset_allowed_brand import AssetAllowedBrand
    from app.models.asset_ai_analysis import AssetAIAnalysis
    from app.models.asset_keyword import AssetKeyword
    from app.models.asset_tag import AssetTag
    from app.models.asset_usage import AssetUsage
    from app.models.brand import Brand
    from app.models.collection import Collection
    from app.models.niche import Niche
    from app.models.product import Product
    from app.models.source import Source

ASSET_TYPE_VALUES = ("video", "image", "audio", "unknown")
ASSET_STATUS_VALUES = ("active", "inactive", "missing", "archived")
SOURCE_STATUS_VALUES = ("active", "missing", "inaccessible", "deleted", "moved", "changed")
PREVIEW_STATUS_VALUES = ("pending", "processing", "ready", "failed", "skipped")
TECHNICAL_METADATA_STATUS_VALUES = ("pending", "processing", "ready", "failed", "skipped")
AI_ENRICHMENT_STATUS_VALUES = ("pending", "processing", "ready", "failed", "needs_review", "skipped")
ORIENTATION_VALUES = ("9:16", "16:9", "square", "unknown")
OVERLAY_SAFE_AREA_VALUES = ("top", "center", "bottom", "left", "right", "full", "unknown")
SHOT_TYPE_VALUES = ("closeup", "medium", "wide", "detail", "establishing", "unknown")
CAMERA_MOTION_VALUES = ("static", "handheld", "pan", "tilt", "zoom", "tracking", "drone", "unknown")
SUBJECT_POSITION_VALUES = ("center", "left", "right", "top", "bottom", "full_frame", "unknown")
VISUAL_ENERGY_VALUES = ("low", "medium", "high", "unknown")
PACING_VALUES = ("slow", "medium", "fast", "unknown")
BEST_SCENE_ROLE_VALUES = (
    "hook",
    "intro",
    "explanation",
    "emotional_peak",
    "transition",
    "broll",
    "outro",
    "background",
    "unknown",
)
USAGE_SCOPE_VALUES = (
    "global",
    "brand_exclusive",
    "allowed_brands",
    "collection_only",
    "restricted",
)
RIGHTS_STATUS_VALUES = ("owned", "licensed", "stock", "unknown", "restricted")
SEGMENTATION_STATUS_VALUES = ("pending", "processing", "ready", "failed", "partial", "skipped")
ORIGINAL_DELETE_STATUS_VALUES = ("pending", "deleted", "failed", "skipped")


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("source_id", "remote_path", name="uq_assets_source_id_remote_path"),
        CheckConstraint(f"provider in {PROVIDER_VALUES}", name="ck_assets_provider"),
        CheckConstraint(f"type in {ASSET_TYPE_VALUES}", name="ck_assets_type"),
        CheckConstraint(f"status in {ASSET_STATUS_VALUES}", name="ck_assets_status"),
        CheckConstraint(
            f"source_status in {SOURCE_STATUS_VALUES}",
            name="ck_assets_source_status",
        ),
        CheckConstraint(
            f"preview_status in {PREVIEW_STATUS_VALUES}",
            name="ck_assets_preview_status",
        ),
        CheckConstraint(
            f"technical_metadata_status in {TECHNICAL_METADATA_STATUS_VALUES}",
            name="ck_assets_technical_metadata_status",
        ),
        CheckConstraint(
            f"ai_enrichment_status in {AI_ENRICHMENT_STATUS_VALUES}",
            name="ck_assets_ai_enrichment_status",
        ),
        CheckConstraint(f"orientation in {ORIENTATION_VALUES}", name="ck_assets_orientation"),
        CheckConstraint(
            f"overlay_safe_area in {OVERLAY_SAFE_AREA_VALUES}",
            name="ck_assets_overlay_safe_area",
        ),
        CheckConstraint(f"shot_type in {SHOT_TYPE_VALUES}", name="ck_assets_shot_type"),
        CheckConstraint(
            f"camera_motion in {CAMERA_MOTION_VALUES}",
            name="ck_assets_camera_motion",
        ),
        CheckConstraint(
            f"subject_position in {SUBJECT_POSITION_VALUES}",
            name="ck_assets_subject_position",
        ),
        CheckConstraint(f"visual_energy in {VISUAL_ENERGY_VALUES}", name="ck_assets_visual_energy"),
        CheckConstraint(f"pacing in {PACING_VALUES}", name="ck_assets_pacing"),
        CheckConstraint(
            f"best_scene_role in {BEST_SCENE_ROLE_VALUES}",
            name="ck_assets_best_scene_role",
        ),
        CheckConstraint(
            f"usage_scope in {USAGE_SCOPE_VALUES}",
            name="ck_assets_usage_scope",
        ),
        CheckConstraint(
            f"rights_status in {RIGHTS_STATUS_VALUES}",
            name="ck_assets_rights_status",
        ),
        CheckConstraint(
            f"segmentation_status in {SEGMENTATION_STATUS_VALUES}",
            name="ck_assets_segmentation_status",
        ),
        CheckConstraint(
            f"original_delete_status in {ORIGINAL_DELETE_STATUS_VALUES}",
            name="ck_assets_original_delete_status",
        ),
        Index("ix_assets_type", "type"),
        Index("ix_assets_status", "status"),
        Index("ix_assets_brand_id", "brand_id"),
        Index("ix_assets_product_id", "product_id"),
        Index("ix_assets_orientation", "orientation"),
        Index("ix_assets_usage_scope", "usage_scope"),
        Index("ix_assets_auto_select_enabled", "auto_select_enabled"),
        Index("ix_assets_filename", "filename"),
        Index("ix_assets_last_used_at", "last_used_at"),
        Index("ix_assets_source_id_remote_path", "source_id", "remote_path"),
        Index("ix_assets_source_status", "source_status"),
        Index("ix_assets_source_id_remote_file_id", "source_id", "remote_file_id"),
        Index("ix_assets_source_video", "source_video"),
        Index("ix_assets_parent_asset_id", "parent_asset_id"),
        Index("ix_assets_is_derivative", "is_derivative"),
        Index("ix_assets_segmentation_status", "segmentation_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_uid: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    rclone_remote: Mapped[str | None] = mapped_column(String(255))
    remote_path: Mapped[str] = mapped_column(String(1200), nullable=False)
    source_video: Mapped[str | None] = mapped_column(String(1200))
    source_path: Mapped[str | None] = mapped_column(String(1200))
    drive_file_id: Mapped[str | None] = mapped_column(String(255))
    remote_file_id: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_ext: Mapped[str | None] = mapped_column(String(40))
    mime_type: Mapped[str | None] = mapped_column(String(180))
    type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    visual_description: Mapped[str | None] = mapped_column(Text)
    action_description: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(String(160))
    people: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(300))
    best_for: Mapped[str | None] = mapped_column(Text)
    avoid_for: Mapped[str | None] = mapped_column(Text)
    orientation: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_hash: Mapped[str | None] = mapped_column(String(160))
    checksum: Mapped[str | None] = mapped_column(String(160))
    quality_score: Mapped[float | None] = mapped_column(Float)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    source_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    source_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_missing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preview_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    preview_path: Mapped[str | None] = mapped_column(String(1200))
    thumbnail_path: Mapped[str | None] = mapped_column(String(1200))
    preview_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preview_error: Mapped[str | None] = mapped_column(Text)
    technical_metadata_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    probe_error: Mapped[str | None] = mapped_column(Text)
    fps: Mapped[float | None] = mapped_column(Float)
    codec: Mapped[str | None] = mapped_column(String(120))
    has_audio: Mapped[bool | None] = mapped_column(Boolean)
    ai_enrichment_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    ai_enrichment_confidence: Mapped[float | None] = mapped_column(Float)
    ai_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_model: Mapped[str | None] = mapped_column(String(160))
    ai_error: Mapped[str | None] = mapped_column(Text)
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    review_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    enrichment_version: Mapped[str | None] = mapped_column(String(80))
    search_text: Mapped[str | None] = mapped_column(Text)
    embedding_text: Mapped[str | None] = mapped_column(Text)
    negative_keywords: Mapped[str | None] = mapped_column(Text)
    auto_keywords_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    usage_scope: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="global",
        server_default=text("'global'"),
    )
    rights_status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    auto_select_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    parent_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    is_derivative: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    derivative_type: Mapped[str | None] = mapped_column(String(80))
    segment_index: Mapped[int | None] = mapped_column(Integer)
    segment_start_seconds: Mapped[float | None] = mapped_column(Float)
    segment_end_seconds: Mapped[float | None] = mapped_column(Float)
    segmentation_status: Mapped[str | None] = mapped_column(String(32))
    segmented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segment_count: Mapped[int | None] = mapped_column(Integer)
    delete_original_eligible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    original_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    original_delete_status: Mapped[str | None] = mapped_column(String(32))
    original_delete_error: Mapped[str | None] = mapped_column(Text)
    segmentation_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segmentation_approved_by: Mapped[str | None] = mapped_column(String(160))

    flip_horizontal_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    flip_vertical_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    crop_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    zoom_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    speed_change_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    reverse_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    color_grade_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    has_visible_text: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    visible_text: Mapped[str | None] = mapped_column(Text)
    text_language: Mapped[str | None] = mapped_column(String(40))
    has_logo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    has_watermark: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    has_faces: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    has_hands: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    has_product: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    has_directional_motion: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )

    safe_for_subtitles: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    safe_for_text_overlay: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    overlay_safe_area: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )

    shot_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    camera_motion: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    subject_position: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    visual_energy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    pacing: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    best_scene_role: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    loopable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    similarity_group: Mapped[str | None] = mapped_column(String(160))
    reuse_cooldown_days: Mapped[int | None] = mapped_column(Integer)

    source: Mapped["Source"] = relationship(back_populates="assets")
    parent_asset: Mapped["Asset | None"] = relationship(
        "Asset",
        remote_side=[id],
        back_populates="derived_assets",
    )
    derived_assets: Mapped[list["Asset"]] = relationship(
        "Asset",
        back_populates="parent_asset",
    )
    brand: Mapped["Brand | None"] = relationship(back_populates="assets")
    product: Mapped["Product | None"] = relationship(back_populates="assets")
    tags: Mapped[list["AssetTag"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    keywords: Mapped[list["AssetKeyword"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    ai_analyses: Mapped[list["AssetAIAnalysis"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    allowed_brands: Mapped[list["AssetAllowedBrand"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    usages: Mapped[list["AssetUsage"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    niches: Mapped[list["Niche"]] = relationship(
        secondary="asset_niches",
        back_populates="assets",
    )
    collections: Mapped[list["Collection"]] = relationship(
        secondary="asset_collections",
        back_populates="assets",
    )
