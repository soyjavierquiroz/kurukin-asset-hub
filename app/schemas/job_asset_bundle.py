from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    selected_asset_uids: list[str] = Field(default_factory=list)
    require_preview_ready: bool = True
    require_ai_ready: bool = False
    allow_needs_review: bool = True
    max_per_similarity_group: int = Field(default=1, ge=1, le=50)

    @model_validator(mode="after")
    def validate_explicit_selection(self) -> "SceneAssetRequest":
        if len(self.selected_asset_uids) != len(set(self.selected_asset_uids)):
            raise ValueError(
                f"selected_asset_uids contains duplicate asset_uid in scene {self.scene_id}"
            )
        if self.selected_asset_uids and "count" in self.model_fields_set:
            selected_count = len(self.selected_asset_uids)
            if self.count != selected_count:
                raise ValueError(
                    f"count contradicts selected_asset_uids in scene {self.scene_id}: "
                    f"count={self.count}, selected_asset_uids={selected_count}"
                )
        return self


class CreateJobAssetBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=160)
    brand_slug: str | None = Field(default=None, min_length=1, max_length=160)
    product_slug: str | None = Field(default=None, max_length=160)
    niche_slug: str | None = Field(default=None, max_length=160)
    scenes: list[SceneAssetRequest] = Field(min_length=1)
    global_exclude_asset_ids: list[int] = Field(default_factory=list)
    global_exclude_asset_uids: list[str] = Field(default_factory=list)
    force: bool = False
    created_by: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_explicit_exclusions(self) -> "CreateJobAssetBundleRequest":
        if self.brand_slug is None and any(not scene.selected_asset_uids for scene in self.scenes):
            raise ValueError("brand_slug is required when any scene uses auto-selection")
        global_excluded_uids = set(self.global_exclude_asset_uids)
        for scene in self.scenes:
            selected_uids = set(scene.selected_asset_uids)
            excluded_uids = global_excluded_uids.union(scene.exclude_asset_uids)
            overlap = sorted(selected_uids.intersection(excluded_uids))
            if overlap:
                raise ValueError(
                    f"selected_asset_uids conflicts with excluded asset_uid in scene "
                    f"{scene.scene_id}: {', '.join(overlap)}"
                )
        return self


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
    score: float | None
    match_reasons: list[str]


class JobAssetBundleResponse(BaseModel):
    bundle_uid: str
    job_id: str
    status: str
    brand_slug: str | None
    product_slug: str | None
    total_scenes: int
    total_assets: int
    assets: list[SelectedBundleAsset]
    manifest: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    error: str | None
