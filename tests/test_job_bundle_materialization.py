from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Asset, Brand, JobAssetBundle, JobAssetBundleItem, Product
from app.services.job_bundle_materialization import (
    JobBundleMaterializationConfigError,
    materialize_job_asset_bundle,
    resolve_rclone_source_path,
    safe_materialized_filename,
    sanitize_materialization_error,
)
from app.services.job_asset_bundles import create_job_asset_bundle
from app.schemas.job_asset_bundle import CreateJobAssetBundleRequest
from app.services.rclone_service import RcloneError
from scripts import materialize_job_bundle as materialize_cli
from tests.test_asset_search_api import API_HEADERS, make_test_client
from tests.test_job_asset_bundles_api import mpt_bundle_payload, seed_mpt_explicit_assets
from tests.test_asset_selection_api import seed_selection_assets


class FakeRclone:
    def __init__(self, fail_paths: set[str] | None = None, fail_all: bool = False) -> None:
        self.fail_paths = fail_paths or set()
        self.fail_all = fail_all
        self.calls: list[tuple[str, str, str]] = []

    def copyto(self, remote: str, remote_path: str, local_path: str, timeout: int = 900) -> None:
        self.calls.append((remote, remote_path, local_path))
        if self.fail_all or remote_path in self.fail_paths:
            raise RcloneError(
                "rclone copyto failed: token=super-secret OPENAI_API_KEY=also-secret"
            )
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(f"{remote}:{remote_path}".encode())


def seed_client() -> tuple[TestClient, sessionmaker[Session]]:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_selection_assets(session)
    return client, session_factory


def configure_storage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOB_ASSETS_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_ASSET_MATERIALIZATION_ENABLED", "true")
    get_settings.cache_clear()


def create_materialization_bundle(
    session: Session,
    *,
    bundle_uid: str = "jab_test_materialize",
    duplicate_asset: bool = False,
) -> JobAssetBundle:
    brand = session.scalar(select(Brand).where(Brand.slug == "grandiosa_mujer"))
    product = session.scalar(select(Product).where(Product.slug == "veyra"))
    assert brand is not None
    assert product is not None
    mystic = session.scalar(select(Asset).where(Asset.asset_uid == "asset-mystic"))
    phone = session.scalar(select(Asset).where(Asset.asset_uid == "asset-phone"))
    assert mystic is not None
    assert phone is not None
    assets = [mystic, mystic if duplicate_asset else phone]
    bundle = JobAssetBundle(
        bundle_uid=bundle_uid,
        job_id="mpt-test-veyra-001",
        brand=brand,
        product=product,
        status="ready",
        request_json={},
        manifest_json={
            "bundle_uid": bundle_uid,
            "job_id": "mpt-test-veyra-001",
            "brand_slug": "grandiosa_mujer",
            "product_slug": "veyra",
            "scenes": [
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "script_scene": "Veyra energia mistica",
                    "assets": [],
                },
                {
                    "scene_id": "scene-002",
                    "scene_index": 2,
                    "script_scene": "Veyra telefono mistico",
                    "assets": [],
                },
            ],
        },
        total_scenes=2,
        total_assets=2,
    )
    bundle.items = [
        make_bundle_item(asset=assets[0], scene_id="scene-001", scene_index=1, rank=1),
        make_bundle_item(asset=assets[1], scene_id="scene-002", scene_index=2, rank=1),
    ]
    session.add(bundle)
    session.commit()
    return bundle


def make_bundle_item(
    asset: Asset,
    scene_id: str,
    scene_index: int,
    rank: int,
) -> JobAssetBundleItem:
    return JobAssetBundleItem(
        scene_id=scene_id,
        scene_index=scene_index,
        asset=asset,
        asset_uid=asset.asset_uid,
        score=0.91,
        rank=rank,
        match_reasons=["keyword match"],
        selection_json={
            "scene_id": scene_id,
            "scene_index": scene_index,
            "rank": rank,
            "id": asset.id,
            "asset_uid": asset.asset_uid,
            "filename": asset.filename,
            "type": asset.type,
            "rclone_remote": asset.rclone_remote,
            "remote_path": asset.remote_path,
            "duration_seconds": asset.duration_seconds,
            "orientation": asset.orientation,
            "width": asset.width,
            "height": asset.height,
            "score": 0.91,
            "match_reasons": ["keyword match"],
            "needs_human_review": asset.needs_human_review,
        },
    )


def test_materialize_bundle_ready_with_two_assets(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()
    fake = FakeRclone()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=fake,
            require_rclone_config=False,
        )
        session.commit()

    assert result["materialization_status"] == "ready"
    assert result["materialized_assets"] == 2
    assert result["failed_assets"] == 0
    assert len(fake.calls) == 2


def test_force_false_reuses_ready_manifest(monkeypatch, tmp_path: Path) -> None:
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
        second_fake = FakeRclone(fail_all=True)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=second_fake,
            require_rclone_config=False,
        )

    assert result["materialization_status"] == "ready"
    assert second_fake.calls == []


def test_force_true_rematerializes(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()
    first_fake = FakeRclone()
    second_fake = FakeRclone()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=first_fake,
            require_rclone_config=False,
        )
        session.commit()
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            force=True,
            rclone_service=second_fake,
            require_rclone_config=False,
        )
        session.commit()

    assert result["materialization_status"] == "ready"
    assert len(first_fake.calls) == 2
    assert len(second_fake.calls) == 2


def test_duplicate_asset_is_copied_once_and_reuses_relative_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()
    fake = FakeRclone()

    with session_factory() as session:
        bundle = create_materialization_bundle(session, duplicate_asset=True)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=fake,
            require_rclone_config=False,
        )
        session.commit()
        relative_paths = [asset["relative_path"] for asset in result["assets"]]

    assert len(fake.calls) == 1
    assert relative_paths[0] == relative_paths[1]


def test_resolve_rclone_source_path_prefixes_relative_path() -> None:
    assert (
        resolve_rclone_source_path(
            "10_genericos/video/foo.mp4",
            "Javier/KURUKIN_ASSET_HUB_PILOT",
        )
        == "Javier/KURUKIN_ASSET_HUB_PILOT/10_genericos/video/foo.mp4"
    )


def test_resolve_rclone_source_path_does_not_duplicate_root() -> None:
    assert (
        resolve_rclone_source_path(
            "Javier/KURUKIN_ASSET_HUB_PILOT/10_genericos/video/foo.mp4",
            "Javier/KURUKIN_ASSET_HUB_PILOT",
        )
        == "Javier/KURUKIN_ASSET_HUB_PILOT/10_genericos/video/foo.mp4"
    )


def test_resolve_rclone_source_path_keeps_path_when_root_empty() -> None:
    assert resolve_rclone_source_path("10_genericos/video/foo.mp4", "") == "10_genericos/video/foo.mp4"


def test_materialize_uses_source_root_path_for_rclone_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()
    fake = FakeRclone()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        item = bundle.items[0]
        item.asset.source.root_path = "Javier/KURUKIN_ASSET_HUB_PILOT"
        item.asset.remote_path = "10_genericos/video/foo.mp4"
        item.asset.filename = "foo.mp4"
        item.selection_json["remote_path"] = "10_genericos/video/foo.mp4"
        item.selection_json["filename"] = "foo.mp4"
        materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=fake,
            require_rclone_config=False,
        )

    assert fake.calls[0][1] == "Javier/KURUKIN_ASSET_HUB_PILOT/10_genericos/video/foo.mp4"


def test_one_asset_failure_marks_bundle_partial(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        fail_path = bundle.items[1].asset.remote_path
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=FakeRclone(fail_paths={fail_path}),
            require_rclone_config=False,
        )
        session.commit()

    assert result["materialization_status"] == "partial"
    assert result["materialized_assets"] == 1
    assert result["failed_assets"] == 1


def test_all_asset_failures_mark_bundle_failed(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=FakeRclone(fail_all=True),
            require_rclone_config=False,
        )

    assert result["materialization_status"] == "failed"
    assert result["materialized_assets"] == 0
    assert result["failed_assets"] == 2


def test_item_saves_local_path_relative_path_size_and_sha256(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
        item = session.scalar(
            select(JobAssetBundleItem)
            .join(JobAssetBundle)
            .where(JobAssetBundle.bundle_uid == bundle.bundle_uid)
        )

    assert item is not None
    assert item.local_path
    assert item.relative_path.startswith("assets/")
    assert item.materialized_size_bytes is not None
    assert item.materialized_size_bytes > 0
    assert item.materialized_sha256 is not None
    assert len(item.materialized_sha256) == 64


def test_renderer_manifest_json_is_saved(monkeypatch, tmp_path: Path) -> None:
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
        saved = session.scalar(
            select(JobAssetBundle).where(JobAssetBundle.bundle_uid == bundle.bundle_uid)
        )

    assert saved is not None
    assert saved.renderer_manifest_json is not None
    assert saved.renderer_manifest_json["bundle_uid"] == bundle.bundle_uid
    manifest_file = tmp_path / bundle.bundle_uid / "manifests" / "renderer-manifest.json"
    assert manifest_file.exists()


def test_explicit_bundle_materialization_and_renderer_manifest_keep_asset_uid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configure_storage(monkeypatch, tmp_path)
    _, session_factory = make_test_client()
    fake = FakeRclone()

    with session_factory() as session:
        seed_mpt_explicit_assets(session)
        request = CreateJobAssetBundleRequest.model_validate(
            mpt_bundle_payload(job_id="mpt-materialize")
        )
        bundle = create_job_asset_bundle(session, request)
        result = materialize_job_asset_bundle(
            session,
            bundle.bundle_uid,
            rclone_service=fake,
            require_rclone_config=False,
        )
        session.commit()

    manifest_assets = [
        asset
        for scene in result["renderer_manifest"]["scenes"]
        for asset in scene["assets"]
    ]
    assert result["materialization_status"] == "ready"
    assert [asset["asset_uid"] for asset in manifest_assets] == ["drive-A", "drive-B"]
    assert all(asset["local_path"] for asset in manifest_assets)
    assert all(asset["relative_path"] for asset in manifest_assets)
    assert all(asset["size_bytes"] for asset in manifest_assets)
    assert all(asset["sha256"] for asset in manifest_assets)


def test_safe_filename_avoids_path_traversal() -> None:
    filename = safe_materialized_filename(7, "../asset:uid", "../../secret folder/video?.mp4")

    assert "/" not in filename
    assert "\\" not in filename
    assert filename.endswith(".mp4")
    assert ".." not in filename


def test_api_materialize_requires_api_key() -> None:
    client, session_factory = seed_client()
    with session_factory() as session:
        bundle = create_materialization_bundle(session)

    response = client.post(f"/api/jobs/asset-bundles/{bundle.bundle_uid}/materialize", json={})

    assert response.status_code == 401


def test_get_materialization_requires_api_key() -> None:
    client, session_factory = seed_client()
    with session_factory() as session:
        bundle = create_materialization_bundle(session)

    response = client.get(f"/api/jobs/asset-bundles/{bundle.bundle_uid}/materialization")

    assert response.status_code == 401


def test_get_renderer_manifest_requires_api_key() -> None:
    client, session_factory = seed_client()
    with session_factory() as session:
        bundle = create_materialization_bundle(session)

    response = client.get(f"/api/jobs/asset-bundles/{bundle.bundle_uid}/renderer-manifest")

    assert response.status_code == 401


def test_cli_by_bundle_uid_prints_summary(monkeypatch, capsys) -> None:
    def fake_materialize(session, bundle_uid: str, force: bool = False):
        return {
            "bundle_uid": bundle_uid,
            "job_id": "mpt-test-veyra-001",
            "total_assets": 2,
            "materialized_assets": 2,
            "failed_assets": 0,
            "materialization_status": "ready",
            "materialized_assets_dir": "job-assets/jab_cli",
        }

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

    monkeypatch.setattr(materialize_cli, "materialize_job_asset_bundle", fake_materialize)
    exit_code = materialize_cli.main(
        ["--bundle-uid", "jab_cli"],
        session_factory=lambda: FakeSession(),
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "bundle_uid=jab_cli" in output
    assert "materialization_status=ready" in output


def test_missing_rclone_config_returns_clear_api_error(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    client, session_factory = seed_client()
    with session_factory() as session:
        bundle = create_materialization_bundle(session)

    response = client.post(
        f"/api/jobs/asset-bundles/{bundle.bundle_uid}/materialize",
        json={},
        headers=API_HEADERS,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "rclone is not configured for materialization"


def test_missing_rclone_config_raises_clear_service_error(monkeypatch, tmp_path: Path) -> None:
    configure_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    _, session_factory = seed_client()

    with session_factory() as session:
        bundle = create_materialization_bundle(session)
        try:
            materialize_job_asset_bundle(session, bundle.bundle_uid)
        except JobBundleMaterializationConfigError as exc:
            assert str(exc) == "rclone is not configured for materialization"
        else:
            raise AssertionError("expected missing rclone config to fail")


def test_sanitized_errors_do_not_print_secrets() -> None:
    sanitized = sanitize_materialization_error(
        "token=abc123 OPENAI_API_KEY=sk-test ASSET_HUB_API_KEY=hub-secret "
        "RCLONE_CONFIG=/config/rclone/rclone.conf"
    )

    assert "abc123" not in sanitized
    assert "sk-test" not in sanitized
    assert "hub-secret" not in sanitized
    assert "/config/rclone/rclone.conf" not in sanitized
    assert "[redacted]" in sanitized
