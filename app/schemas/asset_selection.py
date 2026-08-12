from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import ORIENTATION_VALUES


AssetSelectionType = Literal["video", "image", "audio", "any"]
AssetSelectionOrientation = Literal[*ORIENTATION_VALUES]


class AssetSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_slug: str = Field(min_length=1)
    product_slug: str | None = None
    niche_slug: str | None = None
    query: str | None = None
    script_scene: str | None = None
    scene_role: str | None = None
    asset_type: AssetSelectionType | None = "video"
    orientation: AssetSelectionOrientation | None = None
    count: int = Field(default=5, ge=1, le=50)
    exclude_asset_ids: list[int] = Field(default_factory=list)
    exclude_asset_uids: list[str] = Field(default_factory=list)
    require_preview_ready: bool = False
    require_ai_ready: bool = False
    allow_needs_review: bool = True
    include_restricted: bool = False
    min_score: float | None = None
    preferred_keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    max_per_similarity_group: int = Field(default=1, ge=1, le=50)


class SelectedAsset(BaseModel):
    id: int
    asset_uid: str
    filename: str
    title: str | None
    type: str
    mime_type: str | None
    brand_slug: str | None
    product_slug: str | None
    source_id: str | None
    rclone_remote: str | None
    remote_path: str
    usage_scope: str
    rights_status: str
    preview_status: str
    ai_enrichment_status: str
    needs_human_review: bool
    review_reason: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    orientation: str
    fps: float | None
    codec: str | None
    has_audio: bool | None
    shot_type: str
    camera_motion: str
    subject_position: str
    visual_energy: str
    pacing: str
    best_scene_role: str
    safe_for_subtitles: bool
    safe_for_text_overlay: bool
    overlay_safe_area: str
    flip_horizontal_allowed: bool
    crop_allowed: bool
    zoom_allowed: bool
    speed_change_allowed: bool
    reverse_allowed: bool
    color_grade_allowed: bool
    loopable: bool
    best_for: str | None
    avoid_for: str | None
    search_text: str | None
    score: float | None
    match_reasons: list[str]
    thumbnail_url: str | None
    preview_url: str | None


class AssetSelectionResponse(BaseModel):
    count: int
    requested_count: int
    brand_slug: str
    product_slug: str | None
    query_used: str
    assets: list[SelectedAsset]
