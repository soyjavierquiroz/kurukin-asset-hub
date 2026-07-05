from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.models import Asset, JobAssetBundle
from app.schemas.renderer_manifest import RendererManifest
from app.services.job_bundle_materialization import materialize_job_asset_bundle
from app.services.renderer_manifest import (
    build_recommended_transform,
    build_render_warnings,
    build_renderer_manifest,
)
from scripts import print_renderer_manifest as print_manifest_cli
from tests.test_asset_search_api import API_HEADERS
from tests.test_job_bundle_materialization import (
    FakeRclone,
    configure_storage,
    create_materialization_bundle,
    seed_client,
)


def test_build_renderer_manifest_includes_manifest_version(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        manifest = build_renderer_manifest(bundle)

    assert manifest["manifest_version"] == "1.0"


def test_renderer_manifest_scenes_are_sorted_by_scene_index(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        bundle.manifest_json["scenes"] = list(reversed(bundle.manifest_json["scenes"]))
        manifest = build_renderer_manifest(bundle)

    assert [scene["scene_index"] for scene in manifest["scenes"]] == [1, 2]


def test_renderer_manifest_assets_are_sorted_by_rank(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        bundle.manifest_json["scenes"] = [bundle.manifest_json["scenes"][0]]
        bundle.items[0].rank = 2
        bundle.items[1].scene_id = bundle.items[0].scene_id
        bundle.items[1].scene_index = bundle.items[0].scene_index
        bundle.items[1].rank = 1
        manifest = build_renderer_manifest(bundle)

    assert [asset["rank"] for asset in manifest["scenes"][0]["assets"]] == [1, 2]


def test_renderer_manifest_asset_includes_materialized_file_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=FakeRclone(),
            require_rclone_config=False,
        )
        session.commit()

    first_asset = result["renderer_manifest"]["scenes"][0]["assets"][0]
    assert first_asset["local_path"]
    assert first_asset["relative_path"]
    assert first_asset["sha256"]


def test_recommended_transform_crop_disabled_uses_fit() -> None:
    transform = build_recommended_transform(
        {
            "crop_allowed": False,
            "safe_for_text_overlay": True,
            "safe_for_subtitles": True,
            "overlay_safe_area": "bottom",
            "flip_horizontal_allowed": True,
            "zoom_allowed": True,
            "speed_change_allowed": True,
        }
    )

    assert transform["crop_mode"] == "fit"


def test_safe_for_subtitles_false_generates_warning(monkeypatch, tmp_path: Path) -> None:
    asset, item = load_first_asset_item(monkeypatch, tmp_path)
    asset.safe_for_subtitles = False

    assert "Not safe for subtitles" in build_render_warnings(asset, item)


def test_needs_human_review_generates_warning(monkeypatch, tmp_path: Path) -> None:
    asset, item = load_first_asset_item(monkeypatch, tmp_path)
    asset.needs_human_review = True

    assert "Asset requires human review" in build_render_warnings(asset, item)


def test_visible_text_generates_warning(monkeypatch, tmp_path: Path) -> None:
    asset, item = load_first_asset_item(monkeypatch, tmp_path)
    asset.has_visible_text = True

    assert "Visible text may conflict with overlays" in build_render_warnings(asset, item)


def test_missing_materialized_file_generates_warning(monkeypatch, tmp_path: Path) -> None:
    asset, item = load_first_asset_item(monkeypatch, tmp_path)
    item.local_path = str(tmp_path / "missing.mp4")

    assert "Materialized file missing" in build_render_warnings(asset, item)


def test_pydantic_schema_validates_valid_manifest(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        manifest = build_renderer_manifest(bundle)

    RendererManifest.model_validate(manifest)


def test_get_renderer_manifest_returns_manifest_version(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    client, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=FakeRclone(),
            require_rclone_config=False,
        )
        session.commit()

    response = client.get(
        f"/api/jobs/asset-bundles/{bundle.bundle_uid}/renderer-manifest",
        headers=API_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["manifest_version"] == "1.0"


def test_renderer_manifest_schema_requires_api_key() -> None:
    client, _ = seed_client()

    unauthorized = client.get("/api/renderer-manifest/schema")
    authorized = client.get("/api/renderer-manifest/schema", headers=API_HEADERS)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["title"] == "RendererManifest"


def test_print_renderer_manifest_cli_validate(monkeypatch, tmp_path: Path, capsys) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=FakeRclone(),
            require_rclone_config=False,
        )
        session.commit()

    exit_code = print_manifest_cli.main(
        ["--bundle-uid", bundle.bundle_uid, "--validate"],
        session_factory=session_factory,
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["manifest_version"] == "1.0"
    assert output["bundle_uid"] == bundle.bundle_uid


def load_first_asset_item(monkeypatch, tmp_path: Path) -> tuple[Asset, object]:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()
    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        stored = session.scalar(
            select(JobAssetBundle).where(JobAssetBundle.bundle_uid == bundle.bundle_uid)
        )
        assert stored is not None
        item = stored.items[0]
        assert item.asset is not None
        return item.asset, item
