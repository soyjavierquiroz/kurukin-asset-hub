from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import Asset, JobAssetBundle, JobAssetBundleItem
from app.schemas.renderer_manifest import RendererManifest


MANIFEST_VERSION = "1.0"
GENERATED_BY = "kurukin-asset-hub"


def build_renderer_manifest(bundle: JobAssetBundle) -> dict[str, Any]:
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generated_by": GENERATED_BY,
        "generated_at": datetime.now(UTC).isoformat(),
        "bundle_uid": bundle.bundle_uid,
        "job_id": bundle.job_id,
        "brand": {
            "slug": bundle.brand.slug if bundle.brand else bundle.manifest_json.get("brand_slug"),
            "name": bundle.brand.name if bundle.brand else None,
        },
        "product": {
            "slug": (
                bundle.product.slug if bundle.product else bundle.manifest_json.get("product_slug")
            ),
            "name": bundle.product.name if bundle.product else None,
        },
        "status": bundle.status,
        "materialization_status": bundle.materialization_status,
        "storage": build_storage_contract(bundle),
        "render_defaults": build_render_defaults(bundle),
        "scenes": build_scene_contracts(bundle),
    }
    validate_renderer_manifest_contract(manifest)
    return manifest


def build_renderer_asset_entry(
    bundle_item: JobAssetBundleItem,
    asset: Asset | None,
) -> dict[str, Any]:
    selection = bundle_item.selection_json or {}
    local_path = bundle_item.local_path
    file_exists = bool(local_path and Path(local_path).is_file())
    entry = {
        "asset_id": bundle_item.asset_id or selection.get("id"),
        "asset_uid": bundle_item.asset_uid or selection.get("asset_uid"),
        "filename": selection.get("filename") or (asset.filename if asset else None),
        "title": asset.title if asset else None,
        "type": selection.get("type") or (asset.type if asset else None),
        "mime_type": asset.mime_type if asset else None,
        "source_id": asset.source.source_id if asset and asset.source else selection.get("source_id"),
        "rclone_remote": selection.get("rclone_remote") or (asset.rclone_remote if asset else None),
        "remote_path": selection.get("remote_path") or (asset.remote_path if asset else None),
        "local_path": local_path,
        "relative_path": bundle_item.relative_path,
        "file_exists": file_exists,
        "size_bytes": bundle_item.materialized_size_bytes,
        "sha256": bundle_item.materialized_sha256,
        "duration_seconds": value_from_selection(selection, "duration_seconds", asset),
        "width": value_from_selection(selection, "width", asset),
        "height": value_from_selection(selection, "height", asset),
        "orientation": selection.get("orientation") or (asset.orientation if asset else None),
        "fps": asset.fps if asset else None,
        "codec": asset.codec if asset else None,
        "has_audio": asset.has_audio if asset else None,
        "rank": bundle_item.rank,
        "score": bundle_item.score,
        "match_reasons": list(bundle_item.match_reasons or selection.get("match_reasons") or []),
        "needs_human_review": bool_attr(asset, "needs_human_review", selection, False),
        "review_reason": asset.review_reason if asset else None,
        "visual_description": asset.visual_description if asset else None,
        "action_description": asset.action_description if asset else None,
        "best_for": asset.best_for if asset else None,
        "avoid_for": asset.avoid_for if asset else None,
        "best_scene_role": asset.best_scene_role if asset else None,
        "shot_type": asset.shot_type if asset else None,
        "camera_motion": asset.camera_motion if asset else None,
        "subject_position": asset.subject_position if asset else None,
        "visual_energy": asset.visual_energy if asset else None,
        "pacing": asset.pacing if asset else None,
        "loopable": bool(asset.loopable) if asset else False,
        "safe_for_subtitles": bool_attr(asset, "safe_for_subtitles", selection, True),
        "safe_for_text_overlay": bool_attr(asset, "safe_for_text_overlay", selection, True),
        "overlay_safe_area": str_attr(asset, "overlay_safe_area", selection, "unknown"),
        "flip_horizontal_allowed": bool_attr(
            asset,
            "flip_horizontal_allowed",
            selection,
            True,
        ),
        "flip_vertical_allowed": bool_attr(asset, "flip_vertical_allowed", selection, False),
        "crop_allowed": bool_attr(asset, "crop_allowed", selection, True),
        "zoom_allowed": bool_attr(asset, "zoom_allowed", selection, True),
        "speed_change_allowed": bool_attr(asset, "speed_change_allowed", selection, True),
        "reverse_allowed": bool_attr(asset, "reverse_allowed", selection, False),
        "color_grade_allowed": bool_attr(asset, "color_grade_allowed", selection, True),
    }
    entry["recommended_transform"] = build_recommended_transform(entry)
    entry["render_warnings"] = build_render_warnings(asset, bundle_item)
    return entry


def build_recommended_transform(asset: Any) -> dict[str, Any]:
    crop_allowed = read_bool(asset, "crop_allowed", True)
    safe_for_text_overlay = read_bool(asset, "safe_for_text_overlay", True)
    safe_for_subtitles = read_bool(asset, "safe_for_subtitles", True)
    overlay_safe_area = read_value(asset, "overlay_safe_area", "unknown")
    return {
        "crop_mode": "fill" if crop_allowed else "fit",
        "scale_mode": resolve_scale_mode(
            crop_allowed=crop_allowed,
            safe_for_text_overlay=safe_for_text_overlay,
        ),
        "allow_flip_horizontal": read_bool(asset, "flip_horizontal_allowed", True),
        "allow_zoom": read_bool(asset, "zoom_allowed", True),
        "allow_speed_change": read_bool(asset, "speed_change_allowed", True),
        "subtitle_safe_area": str(overlay_safe_area) if safe_for_subtitles else "none",
    }


def build_render_warnings(asset: Asset | None, item: JobAssetBundleItem) -> list[str]:
    warnings: list[str] = []
    if read_bool(asset, "needs_human_review", False):
        warnings.append("Asset requires human review")
    if not read_bool(asset, "safe_for_subtitles", True):
        warnings.append("Not safe for subtitles")
    if not read_bool(asset, "safe_for_text_overlay", True):
        warnings.append("Not safe for text overlay")
    if read_bool(asset, "has_visible_text", False):
        warnings.append("Visible text may conflict with overlays")
    if read_bool(asset, "has_watermark", False):
        warnings.append("Watermark detected")
    if item.materialization_status == "failed":
        warnings.append("Asset materialization failed")
    if not item.local_path or not Path(item.local_path).is_file():
        warnings.append("Materialized file missing")
    return warnings


def validate_renderer_manifest_contract(manifest: dict[str, Any]) -> None:
    RendererManifest.model_validate(manifest)


def build_storage_contract(bundle: JobAssetBundle) -> dict[str, str]:
    base_path = Path(get_settings().job_assets_storage_dir)
    storage_dir = base_path / bundle.bundle_uid
    return {
        "storage_dir": str(storage_dir),
        "assets_dir": str(storage_dir / "assets"),
        "manifests_dir": str(storage_dir / "manifests"),
        "base_path": str(base_path),
        "path_mode": "container",
    }


def build_render_defaults(bundle: JobAssetBundle) -> dict[str, Any]:
    scenes = [
        scene
        for scene in bundle.request_json.get("scenes", [])
        if isinstance(scene, dict)
    ]
    orientations = {
        scene.get("orientation")
        for scene in scenes
        if isinstance(scene.get("orientation"), str) and scene.get("orientation")
    }
    allow_values = [
        bool(scene.get("allow_needs_review", True))
        for scene in scenes
    ]
    return {
        "expected_orientation": next(iter(orientations)) if len(orientations) == 1 else None,
        "safe_for_subtitles_required": True,
        "allow_needs_review": all(allow_values) if allow_values else True,
    }


def build_scene_contracts(bundle: JobAssetBundle) -> list[dict[str, Any]]:
    source_scenes = [
        scene
        for scene in bundle.manifest_json.get("scenes", [])
        if isinstance(scene, dict) and scene.get("scene_id") is not None
    ]
    scenes_by_id = {str(scene["scene_id"]): scene for scene in source_scenes}
    scene_groups: dict[str, dict[str, Any]] = {
        str(scene["scene_id"]): {
            "scene_id": str(scene["scene_id"]),
            "scene_index": scene.get("scene_index"),
            "script_scene": scene.get("script_scene"),
            "requested_count": int(scene.get("requested_count") or 0),
            "notes": list(scene.get("notes") or []),
            "assets": [],
        }
        for scene in source_scenes
    }
    for item in ordered_bundle_items(bundle):
        source_scene = scenes_by_id.get(item.scene_id, {})
        scene = scene_groups.setdefault(
            item.scene_id,
            {
                "scene_id": item.scene_id,
                "scene_index": item.scene_index,
                "script_scene": item.selection_json.get("script_scene")
                if isinstance(item.selection_json, dict)
                else None,
                "requested_count": int(source_scene.get("requested_count") or 0),
                "notes": [],
                "assets": [],
            },
        )
        if scene["scene_index"] is None:
            scene["scene_index"] = item.scene_index
        if scene["requested_count"] <= 0:
            scene["requested_count"] = max(1, len(scene["assets"]) + 1)
        scene["assets"].append(build_renderer_asset_entry(item, item.asset))

    scenes = []
    for scene in scene_groups.values():
        scene["assets"] = sorted(
            scene["assets"],
            key=lambda asset: (
                asset["rank"] if asset["rank"] is not None else 999999,
                asset["asset_id"] if asset["asset_id"] is not None else 999999,
                asset["asset_uid"] or "",
            ),
        )
        scene["selected_count"] = len(scene["assets"])
        scene["selection_status"] = resolve_selection_status(
            selected_count=scene["selected_count"],
            requested_count=scene["requested_count"],
        )
        if scene["selected_count"] == 0 and "No assets selected" not in scene["notes"]:
            scene["notes"].append("No assets selected")
        scenes.append(scene)
    return sorted(
        scenes,
        key=lambda scene: (
            scene["scene_index"] if scene["scene_index"] is not None else 999999,
            scene["scene_id"],
        ),
    )


def ordered_bundle_items(bundle: JobAssetBundle) -> list[JobAssetBundleItem]:
    return sorted(
        bundle.items,
        key=lambda item: (
            item.scene_index if item.scene_index is not None else 999999,
            item.scene_id,
            item.rank if item.rank is not None else 999999,
            item.id if item.id is not None else 999999,
        ),
    )


def resolve_selection_status(selected_count: int, requested_count: int) -> str:
    if selected_count == 0:
        return "empty"
    if requested_count > 0 and selected_count < requested_count:
        return "partial"
    return "ready"


def resolve_scale_mode(crop_allowed: bool, safe_for_text_overlay: bool) -> str:
    if not safe_for_text_overlay:
        return "contain"
    if crop_allowed:
        return "cover"
    return "contain"


def value_from_selection(selection: dict[str, Any], key: str, asset: Asset | None) -> Any:
    if selection.get(key) is not None:
        return selection[key]
    return getattr(asset, key) if asset else None


def bool_attr(
    asset: Asset | None,
    name: str,
    selection: dict[str, Any],
    default: bool,
) -> bool:
    if selection.get(name) is not None:
        return bool(selection[name])
    if asset is not None:
        return bool(getattr(asset, name))
    return default


def str_attr(
    asset: Asset | None,
    name: str,
    selection: dict[str, Any],
    default: str,
) -> str:
    if selection.get(name) is not None:
        return str(selection[name])
    if asset is not None:
        return str(getattr(asset, name))
    return default


def read_bool(source: Any, name: str, default: bool) -> bool:
    value = read_value(source, name, default)
    return bool(value)


def read_value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    if source is not None and hasattr(source, name):
        return getattr(source, name)
    return default
