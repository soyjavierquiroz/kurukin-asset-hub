from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import ORIENTATION_VALUES
from app.schemas.asset_selection import AssetSelectionType


class SceneAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(min_length=1, max_length=160)
    scene_index: int | None = None
    query: str | None = None
    script_scene: str = Field(min_length=1)
    scene_role: str | None = None
    asset_type: AssetSelectionType | None = "video"
    orientation: Literal[*ORIENTATION_VALUES] | None = None
    count: int = Field(default=1, ge=1, le=50)
    preferred_keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    exclude_asset_ids: list[int] = Field(default_factory=list)
    exclude_asset_uids: list[str] = Field(default_factory=list)
    require_preview_ready: bool = True
    require_ai_ready: bool = False
    allow_needs_review: bool = True
    max_per_similarity_group: int = Field(default=1, ge=1, le=50)


class CreateJobAssetBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=160)
    brand_slug: str = Field(min_length=1, max_length=160)
    product_slug: str | None = Field(default=None, max_length=160)
    niche_slug: str | None = Field(default=None, max_length=160)
    scenes: list[SceneAssetRequest] = Field(min_length=1)
    global_exclude_asset_ids: list[int] = Field(default_factory=list)
    global_exclude_asset_uids: list[str] = Field(default_factory=list)
    force: bool = False
    created_by: str | None = Field(default=None, max_length=160)


class SelectedBundleAsset(BaseModel):
    scene_id: str
    scene_index: int | None
    rank: int
    id: int
    asset_uid: str
    filename: str
    type: str
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
    duration_seconds: float | None
    width: int | None
    height: int | None
    orientation: str
    thumbnail_url: str | None
    preview_url: str | None
    score: float
    match_reasons: list[str]


class JobAssetBundleResponse(BaseModel):
    bundle_uid: str
    job_id: str
    status: str
    brand_slug: str
    product_slug: str | None
    total_scenes: int
    total_assets: int
    assets: list[SelectedBundleAsset]
    manifest: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    error: str | None
