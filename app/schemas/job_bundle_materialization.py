from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MaterializeBundleRequest(BaseModel):
    force: bool = False


class MaterializedBundleAsset(BaseModel):
    scene_id: str
    scene_index: int | None
    rank: int | None
    asset_id: int | None
    asset_uid: str | None
    filename: str | None
    type: str | None
    local_path: str | None
    relative_path: str | None
    size_bytes: int | None
    sha256: str | None
    materialization_status: str
    materialization_error: str | None
    score: float | None
    match_reasons: list[str]
    rclone_remote: str | None
    remote_path: str | None
    needs_human_review: bool | None


class BundleMaterializationResponse(BaseModel):
    bundle_uid: str
    job_id: str
    status: str
    materialization_status: str
    total_assets: int
    materialized_assets: int
    failed_assets: int
    materialized_at: datetime | None
    materialized_assets_dir: str | None
    renderer_manifest: dict[str, Any] | None
    assets: list[MaterializedBundleAsset]
    error: str | None
