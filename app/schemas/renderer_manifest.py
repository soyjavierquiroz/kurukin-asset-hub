from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RendererManifestBrand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str | None
    name: str | None


class RendererManifestProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str | None
    name: str | None


class RendererManifestStorage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_dir: str
    assets_dir: str
    manifests_dir: str
    base_path: str
    path_mode: Literal["container"]


class RendererManifestDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_orientation: str | None
    safe_for_subtitles_required: bool
    allow_needs_review: bool


class RendererRecommendedTransform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crop_mode: Literal["fit", "fill"]
    scale_mode: Literal["contain", "cover"]
    allow_flip_horizontal: bool
    allow_zoom: bool
    allow_speed_change: bool
    subtitle_safe_area: str


class RendererManifestAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: int | None
    asset_uid: str | None
    filename: str | None
    title: str | None
    type: str | None
    mime_type: str | None
    source_id: str | None
    rclone_remote: str | None
    remote_path: str | None
    local_path: str | None
    relative_path: str | None
    file_exists: bool
    size_bytes: int | None
    sha256: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    orientation: str | None
    fps: float | None
    codec: str | None
    has_audio: bool | None
    rank: int | None
    score: float | None
    match_reasons: list[str]
    needs_human_review: bool
    review_reason: str | None
    visual_description: str | None
    action_description: str | None
    best_for: str | None
    avoid_for: str | None
    best_scene_role: str | None
    shot_type: str | None
    camera_motion: str | None
    subject_position: str | None
    visual_energy: str | None
    pacing: str | None
    loopable: bool
    safe_for_subtitles: bool
    safe_for_text_overlay: bool
    overlay_safe_area: str
    flip_horizontal_allowed: bool
    flip_vertical_allowed: bool
    crop_allowed: bool
    zoom_allowed: bool
    speed_change_allowed: bool
    reverse_allowed: bool
    color_grade_allowed: bool
    recommended_transform: RendererRecommendedTransform
    render_warnings: list[str]


class RendererManifestScene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str
    scene_index: int | None
    script_scene: str | None
    requested_count: int
    selected_count: int
    selection_status: Literal["ready", "partial", "empty"]
    notes: list[str]
    assets: list[RendererManifestAsset]


class RendererManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["1.0"]
    generated_by: Literal["kurukin-asset-hub"]
    generated_at: datetime
    bundle_uid: str
    job_id: str
    brand: RendererManifestBrand
    product: RendererManifestProduct
    status: str
    materialization_status: str
    storage: RendererManifestStorage
    render_defaults: RendererManifestDefaults
    scenes: list[RendererManifestScene]
