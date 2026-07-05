from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import JobAssetBundle, JobAssetBundleItem
from app.services.rclone_service import RcloneError, RcloneService, sanitize_rclone_message


class JobBundleMaterializationError(RuntimeError):
    pass


class JobBundleMaterializationNotFoundError(JobBundleMaterializationError):
    pass


class JobBundleMaterializationConfigError(JobBundleMaterializationError):
    pass


class JobBundleMaterializationValidationError(JobBundleMaterializationError):
    pass


def materialize_job_asset_bundle(
    session: Session,
    bundle_uid: str,
    force: bool = False,
    rclone_service: RcloneService | None = None,
    require_rclone_config: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.job_asset_materialization_enabled:
        raise JobBundleMaterializationConfigError("job asset materialization is disabled")
    if require_rclone_config:
        require_configured_rclone()

    bundle = load_bundle(session, bundle_uid)
    if bundle is None:
        raise JobBundleMaterializationNotFoundError("Bundle not found")
    if not bundle.items:
        raise JobBundleMaterializationValidationError("Bundle has no selected assets")
    if (
        bundle.materialization_status == "ready"
        and bundle.renderer_manifest_json is not None
        and not force
    ):
        return build_materialization_response(bundle)

    rclone = rclone_service or RcloneService()
    now = datetime.now(UTC)
    bundle_dir = get_bundle_storage_dir(bundle.bundle_uid)
    assets_dir = bundle_dir / "assets"
    manifests_dir = bundle_dir / "manifests"

    if force and bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    bundle.materialization_status = "processing"
    bundle.materialization_error = None
    for item in bundle.items:
        item.materialization_status = "processing"
        item.materialization_error = None
        if force:
            item.local_path = None
            item.relative_path = None
            item.materialized_size_bytes = None
            item.materialized_sha256 = None
            item.materialized_at = None
    session.flush()

    copied_assets: dict[str, dict[str, Any]] = {}
    for item in ordered_bundle_items(bundle):
        try:
            materialize_bundle_item(
                item=item,
                bundle_dir=bundle_dir,
                assets_dir=assets_dir,
                rclone_service=rclone,
                copied_assets=copied_assets,
            )
        except Exception as exc:
            item.materialization_status = "failed"
            item.materialization_error = sanitize_materialization_error(str(exc))
            item.materialized_at = datetime.now(UTC)

    ready_count = sum(1 for item in bundle.items if item.materialization_status == "ready")
    failed_count = sum(1 for item in bundle.items if item.materialization_status == "failed")
    if ready_count == len(bundle.items):
        bundle.materialization_status = "ready"
        bundle.materialization_error = None
    elif ready_count > 0 and failed_count > 0:
        bundle.materialization_status = "partial"
        bundle.materialization_error = f"{failed_count} asset(s) failed to materialize"
    else:
        bundle.materialization_status = "failed"
        bundle.materialization_error = "No assets were materialized"

    bundle.materialized_assets_dir = f"job-assets/{bundle.bundle_uid}"
    bundle.materialized_at = now
    manifest = build_renderer_manifest(bundle)
    bundle.renderer_manifest_json = manifest
    manifest_path = manifests_dir / "renderer-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    session.flush()
    return build_materialization_response(bundle)


def get_bundle_materialization(session: Session, bundle_uid: str) -> dict[str, Any]:
    bundle = load_bundle(session, bundle_uid)
    if bundle is None:
        raise JobBundleMaterializationNotFoundError("Bundle not found")
    return build_materialization_response(bundle)


def build_renderer_manifest(bundle: JobAssetBundle) -> dict[str, Any]:
    scenes_by_id = {
        str(scene.get("scene_id")): scene
        for scene in bundle.manifest_json.get("scenes", [])
        if isinstance(scene, dict) and scene.get("scene_id") is not None
    }
    scene_groups: dict[tuple[int, str], dict[str, Any]] = {}
    for item in ordered_bundle_items(bundle):
        scene_index = item.scene_index if item.scene_index is not None else 0
        key = (scene_index, item.scene_id)
        source_scene = scenes_by_id.get(item.scene_id, {})
        scene = scene_groups.setdefault(
            key,
            {
                "scene_id": item.scene_id,
                "scene_index": item.scene_index,
                "script_scene": source_scene.get("script_scene"),
                "assets": [],
            },
        )
        scene["assets"].append(serialize_manifest_item(item))

    return {
        "bundle_uid": bundle.bundle_uid,
        "job_id": bundle.job_id,
        "brand_slug": bundle.brand.slug if bundle.brand else bundle.manifest_json.get("brand_slug"),
        "product_slug": (
            bundle.product.slug if bundle.product else bundle.manifest_json.get("product_slug")
        ),
        "materialized_at": (
            bundle.materialized_at.isoformat() if bundle.materialized_at else None
        ),
        "storage_dir": bundle.materialized_assets_dir,
        "scenes": [scene_groups[key] for key in sorted(scene_groups)],
    }


def safe_materialized_filename(asset_id: int | None, asset_uid: str | None, filename: str) -> str:
    basename = Path(filename or "asset").name
    stem = Path(basename).stem or "asset"
    suffix = Path(basename).suffix.lower()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "asset"
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix) else ""
    safe_asset_uid = re.sub(r"[^A-Za-z0-9._-]+", "_", asset_uid or "asset").strip("._-")
    safe_asset_uid = safe_asset_uid or "asset"
    safe_asset_id = str(asset_id) if asset_id is not None else "asset"
    return f"{safe_asset_id}_{safe_asset_uid}_{safe_stem}{safe_suffix}"


def get_bundle_storage_dir(bundle_uid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", bundle_uid):
        raise JobBundleMaterializationValidationError("Invalid bundle_uid")
    root = Path(get_settings().job_assets_storage_dir)
    bundle_dir = root / bundle_uid
    root_resolved = root.resolve(strict=False)
    bundle_resolved = bundle_dir.resolve(strict=False)
    if root_resolved not in [bundle_resolved, *bundle_resolved.parents]:
        raise JobBundleMaterializationValidationError("Invalid materialization path")
    return bundle_dir


def materialize_bundle_item(
    item: JobAssetBundleItem,
    bundle_dir: Path,
    assets_dir: Path,
    rclone_service: RcloneService,
    copied_assets: dict[str, dict[str, Any]],
) -> None:
    asset = item.asset
    selection = item.selection_json or {}
    remote = (
        asset.rclone_remote
        if asset and asset.rclone_remote
        else selection.get("rclone_remote")
    )
    remote_path = asset.remote_path if asset and asset.remote_path else selection.get("remote_path")
    filename = asset.filename if asset and asset.filename else selection.get("filename")
    if not remote or not isinstance(remote, str):
        raise JobBundleMaterializationValidationError("Asset has no rclone remote")
    if not remote_path or not isinstance(remote_path, str):
        raise JobBundleMaterializationValidationError("Asset has no remote path")
    if not filename or not isinstance(filename, str):
        filename = Path(remote_path).name

    dedupe_key = str(item.asset_id or item.asset_uid or f"{remote}:{remote_path}")
    if dedupe_key in copied_assets:
        copied = copied_assets[dedupe_key]
        set_item_materialized_fields(item, copied)
        return

    safe_filename = safe_materialized_filename(item.asset_id, item.asset_uid, filename)
    rank = item.rank if item.rank is not None else item.id
    local_path = assets_dir / f"{rank}_{safe_filename}"
    ensure_child_path(local_path, bundle_dir)
    relative_path = local_path.relative_to(bundle_dir).as_posix()

    try:
        rclone_service.copyto(remote, remote_path, str(local_path), timeout=900)
    except RcloneError as exc:
        raise JobBundleMaterializationValidationError(str(exc)) from exc

    size_bytes = local_path.stat().st_size
    sha256 = compute_sha256(local_path)
    copied = {
        "local_path": str(local_path),
        "relative_path": relative_path,
        "materialized_size_bytes": size_bytes,
        "materialized_sha256": sha256,
        "materialized_at": datetime.now(UTC),
    }
    copied_assets[dedupe_key] = copied
    set_item_materialized_fields(item, copied)


def compute_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle(session: Session, bundle_uid: str) -> JobAssetBundle | None:
    return session.scalar(
        select(JobAssetBundle)
        .where(JobAssetBundle.bundle_uid == bundle_uid)
        .options(
            selectinload(JobAssetBundle.brand),
            selectinload(JobAssetBundle.product),
            selectinload(JobAssetBundle.items).selectinload(JobAssetBundleItem.asset),
        )
    )


def ordered_bundle_items(bundle: JobAssetBundle) -> list[JobAssetBundleItem]:
    return sorted(
        bundle.items,
        key=lambda item: (
            item.scene_index if item.scene_index is not None else 999999,
            item.scene_id,
            item.rank if item.rank is not None else 999999,
            item.id,
        ),
    )


def serialize_manifest_item(item: JobAssetBundleItem) -> dict[str, Any]:
    selection = item.selection_json or {}
    asset = item.asset
    return {
        "asset_id": item.asset_id or selection.get("id"),
        "asset_uid": item.asset_uid or selection.get("asset_uid"),
        "filename": selection.get("filename") or (asset.filename if asset else None),
        "scene_id": item.scene_id,
        "rank": item.rank,
        "type": selection.get("type") or (asset.type if asset else None),
        "duration_seconds": selection.get("duration_seconds")
        if selection.get("duration_seconds") is not None
        else (asset.duration_seconds if asset else None),
        "orientation": selection.get("orientation") or (asset.orientation if asset else None),
        "width": (
            selection.get("width")
            if selection.get("width") is not None
            else (asset.width if asset else None)
        ),
        "height": (
            selection.get("height")
            if selection.get("height") is not None
            else (asset.height if asset else None)
        ),
        "score": item.score,
        "match_reasons": item.match_reasons or selection.get("match_reasons") or [],
        "rclone_remote": selection.get("rclone_remote") or (asset.rclone_remote if asset else None),
        "remote_path": selection.get("remote_path") or (asset.remote_path if asset else None),
        "local_path": item.local_path,
        "relative_path": item.relative_path,
        "size_bytes": item.materialized_size_bytes,
        "sha256": item.materialized_sha256,
        "needs_human_review": selection.get("needs_human_review")
        if selection.get("needs_human_review") is not None
        else (asset.needs_human_review if asset else None),
        "safe_for_subtitles": asset.safe_for_subtitles if asset else None,
        "safe_for_text_overlay": asset.safe_for_text_overlay if asset else None,
        "overlay_safe_area": asset.overlay_safe_area if asset else None,
        "flip_horizontal_allowed": asset.flip_horizontal_allowed if asset else None,
        "crop_allowed": asset.crop_allowed if asset else None,
        "zoom_allowed": asset.zoom_allowed if asset else None,
        "speed_change_allowed": asset.speed_change_allowed if asset else None,
        "reverse_allowed": asset.reverse_allowed if asset else None,
        "color_grade_allowed": asset.color_grade_allowed if asset else None,
    }


def build_materialization_response(bundle: JobAssetBundle) -> dict[str, Any]:
    assets = [serialize_response_item(item) for item in ordered_bundle_items(bundle)]
    materialized_assets = sum(1 for item in bundle.items if item.materialization_status == "ready")
    failed_assets = sum(1 for item in bundle.items if item.materialization_status == "failed")
    return {
        "bundle_uid": bundle.bundle_uid,
        "job_id": bundle.job_id,
        "status": bundle.status,
        "materialization_status": bundle.materialization_status,
        "total_assets": bundle.total_assets,
        "materialized_assets": materialized_assets,
        "failed_assets": failed_assets,
        "materialized_at": bundle.materialized_at,
        "materialized_assets_dir": bundle.materialized_assets_dir,
        "renderer_manifest": bundle.renderer_manifest_json,
        "assets": assets,
        "error": bundle.materialization_error,
    }


def serialize_response_item(item: JobAssetBundleItem) -> dict[str, Any]:
    selection = item.selection_json or {}
    return {
        "scene_id": item.scene_id,
        "scene_index": item.scene_index,
        "rank": item.rank,
        "asset_id": item.asset_id or selection.get("id"),
        "asset_uid": item.asset_uid or selection.get("asset_uid"),
        "filename": selection.get("filename") or (item.asset.filename if item.asset else None),
        "type": selection.get("type") or (item.asset.type if item.asset else None),
        "local_path": item.local_path,
        "relative_path": item.relative_path,
        "size_bytes": item.materialized_size_bytes,
        "sha256": item.materialized_sha256,
        "materialization_status": item.materialization_status,
        "materialization_error": item.materialization_error,
        "score": item.score,
        "match_reasons": item.match_reasons or selection.get("match_reasons") or [],
        "rclone_remote": selection.get("rclone_remote")
        or (item.asset.rclone_remote if item.asset else None),
        "remote_path": selection.get("remote_path")
        or (item.asset.remote_path if item.asset else None),
        "needs_human_review": selection.get("needs_human_review"),
    }


def set_item_materialized_fields(item: JobAssetBundleItem, copied: dict[str, Any]) -> None:
    item.local_path = copied["local_path"]
    item.relative_path = copied["relative_path"]
    item.materialized_size_bytes = copied["materialized_size_bytes"]
    item.materialized_sha256 = copied["materialized_sha256"]
    item.materialized_at = copied["materialized_at"]
    item.materialization_status = "ready"
    item.materialization_error = None


def require_configured_rclone() -> None:
    config_path = os.environ.get("RCLONE_CONFIG")
    if not config_path or not Path(config_path).is_file():
        raise JobBundleMaterializationConfigError(
            "rclone is not configured for materialization"
        )


def ensure_child_path(path: Path, parent: Path) -> None:
    parent_resolved = parent.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if parent_resolved not in path_resolved.parents:
        raise JobBundleMaterializationValidationError("Invalid destination path")


def sanitize_materialization_error(message: str) -> str:
    sanitized = sanitize_rclone_message(message)
    patterns = [
        r"(?i)(OPENAI_API_KEY=)\S+",
        r"(?i)(ASSET_HUB_API_KEY=)\S+",
        r"(?i)(RCLONE_CONFIG=)\S+",
        r"(?i)(client_secret\s*=\s*)\S+",
    ]
    for pattern in patterns:
        sanitized = re.sub(pattern, r"\1[redacted]", sanitized)
    return sanitized[:1000]
