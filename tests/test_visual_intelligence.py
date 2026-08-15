from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Asset, AssetAIAnalysis, Source
from app.schemas.visual_intelligence import VISUAL_PROFILE_VERSION, VisualIntelligenceResult
from app.services.asset_location import resolve_asset_rclone_location
from app.services import visual_intelligence as visual


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def source(session: Session) -> Source:
    source = Source(
        source_id="visual-test-source",
        provider="google_drive",
        label="Visual Test",
        rclone_remote="gdrive_test",
    )
    session.add(source)
    session.commit()
    return source


def test_extract_temporal_frames_writes_ten_cached_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"video")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    timestamps: list[str] = []

    def fake_run(command: list[str], **_kwargs):
        timestamps.append(command[command.index("-ss") + 1])
        output = Path(command[-1])
        output.write_bytes(b"jpg")
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(visual.subprocess, "run", fake_run)

    frames = visual.extract_temporal_frames(master, output_dir, duration_seconds=11.0)

    assert len(frames) == 10
    assert frames[0] == output_dir / "frame_001.jpg"
    assert timestamps[0] == "1.000"
    assert timestamps[-1] == "10.000"


def test_collect_visual_frames_downloads_master_and_caches_frames(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "cache-frames")
    session.add(asset)
    session.commit()

    def fake_copyto(_self, remote: str, remote_path: str, local_path: str, timeout: int = 900):
        assert remote == "gdrive_test"
        assert remote_path == "videos/cache-frames.mp4"
        Path(local_path).write_bytes(b"master")

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda _master, output_dir, _duration, _count: write_frames(output_dir, 10),
    )

    inputs = visual.collect_visual_frames(asset)

    assert not inputs.master_path.exists()
    assert inputs.frame_cache_dir == tmp_path / "previews" / str(asset.id) / "visual-v1-frames"
    assert len(inputs.frame_paths) == 10
    manifest = inputs.frame_cache_dir / "manifest.json"
    assert manifest.is_file()


def test_resolve_asset_rclone_location_prefixes_catalog_path_with_source_root(
    source: Source,
) -> None:
    source.root_path = "Javier/KURUKIN_ASSET_HUB_PILOT"
    asset = make_asset(source, "catalog-path")
    asset.remote_path = "10_genericos/video/lote-0001/foo.mp4"

    location = resolve_asset_rclone_location(asset)

    assert location.remote == "gdrive_test"
    assert location.remote_path == "Javier/KURUKIN_ASSET_HUB_PILOT/10_genericos/video/lote-0001/foo.mp4"
    assert location.remote_path != "10_genericos/video/lote-0001/foo.mp4"


def test_resolve_asset_rclone_location_moved_asset_uses_current_path_not_old_source_path(
    source: Source,
) -> None:
    source.root_path = "AssetHubRoot"
    asset = make_asset(source, "moved-current")
    asset.remote_path = "10_genericos/video/lote-0001/current.mp4"
    asset.source_path = "00_inbox/original/current.mp4"
    asset.status = "ready"
    asset.move_status = "moved"

    location = resolve_asset_rclone_location(asset)

    assert location.remote_path == "AssetHubRoot/10_genericos/video/lote-0001/current.mp4"
    assert "00_inbox" not in location.remote_path


@pytest.mark.parametrize(
    ("scope", "remote_path"),
    [
        ("generic", "10_genericos/video/lote-0001/foo.mp4"),
        ("title", "30_peliculas_series/series/mi-otra-yo/video/lote-0001/foo.mp4"),
        ("brand", "20_marcas/grandiosa-mujer/evergreen/video/lote-0001/foo.mp4"),
    ],
)
def test_resolve_asset_rclone_location_supports_compact_v3_scopes(
    source: Source,
    scope: str,
    remote_path: str,
) -> None:
    source.root_path = "AssetHubRoot"
    asset = make_asset(source, f"{scope}-asset")
    asset.scope = scope
    asset.path_layout_version = "compact_v3"
    asset.remote_path = remote_path

    location = resolve_asset_rclone_location(asset)

    assert location.remote_path == f"AssetHubRoot/{remote_path}"


def test_resolve_asset_rclone_location_does_not_duplicate_physical_root(
    source: Source,
) -> None:
    source.root_path = "AssetHubRoot"
    asset = make_asset(source, "already-qualified")
    asset.remote_path = "AssetHubRoot/10_genericos/video/lote-0001/foo.mp4"

    location = resolve_asset_rclone_location(asset)

    assert location.remote_path == "AssetHubRoot/10_genericos/video/lote-0001/foo.mp4"


def test_collect_visual_frames_uses_common_asset_location_resolver(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    source.root_path = "AssetHubRoot"
    asset = make_asset(source, "common-resolver")
    asset.remote_path = "10_genericos/video/lote-0001/foo.mp4"
    session.add(asset)
    session.commit()
    calls: list[tuple[str, str]] = []

    def fake_copyto(_self, remote: str, remote_path: str, local_path: str, timeout: int = 900):
        calls.append((remote, remote_path))
        Path(local_path).write_bytes(b"master")

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda _master, output_dir, _duration, _count: write_frames(output_dir, 10),
    )

    visual.collect_visual_frames(asset)

    assert calls == [("gdrive_test", "AssetHubRoot/10_genericos/video/lote-0001/foo.mp4")]


def test_collect_visual_frames_reuses_same_fingerprint_without_rclone(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "reuse-cache")
    asset.source_hash = "hash-1"
    session.add(asset)
    session.commit()
    calls = {"rclone": 0, "ffmpeg": 0}

    def fake_copyto(_self, _remote: str, _remote_path: str, local_path: str, timeout: int = 900):
        calls["rclone"] += 1
        Path(local_path).write_bytes(b"master")

    def fake_extract(_master: Path, output_dir: Path, _duration: float | None, _count: int) -> list[Path]:
        calls["ffmpeg"] += 1
        return write_frames(output_dir, 10)

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(visual, "extract_temporal_frames", fake_extract)

    first = visual.collect_visual_frames(asset)
    second = visual.collect_visual_frames(asset)

    assert len(first.frame_paths) == 10
    assert len(second.frame_paths) == 10
    assert calls == {"rclone": 1, "ffmpeg": 1}


def test_collect_visual_frames_valid_cache_avoids_rclone_and_location_resolution(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "cache-no-rclone")
    asset.source_hash = "hash-1"
    session.add(asset)
    session.commit()
    frame_dir = visual.visual_frame_cache_dir(asset.id)
    frames = write_frames(frame_dir, 10)
    visual.write_visual_frame_manifest(frame_dir, VISUAL_PROFILE_VERSION, visual.source_fingerprint(asset), 11.0, frames)

    def fail_copyto(*_args, **_kwargs):
        raise AssertionError("rclone should not run for a valid frame cache")

    def fail_resolver(_asset):
        raise AssertionError("location resolution should not run for a valid frame cache")

    monkeypatch.setattr(visual.RcloneService, "copyto", fail_copyto)
    monkeypatch.setattr(visual, "resolve_asset_rclone_location", fail_resolver)

    inputs = visual.collect_visual_frames(asset)

    assert len(inputs.frame_paths) == 10


def test_collect_visual_frames_regenerates_when_source_fingerprint_changes(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "fingerprint-cache")
    asset.source_hash = "hash-1"
    session.add(asset)
    session.commit()
    calls = {"rclone": 0}

    def fake_copyto(_self, _remote: str, _remote_path: str, local_path: str, timeout: int = 900):
        calls["rclone"] += 1
        Path(local_path).write_bytes(b"master")

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda _master, output_dir, _duration, _count: write_frames(output_dir, 10),
    )

    visual.collect_visual_frames(asset)
    asset.source_hash = "hash-2"
    visual.collect_visual_frames(asset)

    assert calls["rclone"] == 2


def test_visual_location_failure_is_operational_not_rejected_and_keeps_asset_uid(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    source.root_path = "WrongRoot"
    asset = make_asset(source, "wrong-root")
    original_uid = asset.asset_uid
    session.add(asset)
    session.commit()

    def fake_copyto(_self, _remote: str, remote_path: str, _local_path: str, timeout: int = 900):
        raise RuntimeError(f"directory not found: {remote_path}")

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)

    analyzed = visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        force=True,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    analysis = latest_analysis(session, analyzed)
    assert analysis.result_json["status"] == "failed"
    assert analysis.result_json["error_type"] == "operational_failure"
    assert analyzed.editorial_status != "rejected"
    assert analyzed.asset_uid == original_uid


def test_visual_v2_uses_separate_frame_cache_dir(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "v2-cache")
    session.add(asset)
    session.commit()

    monkeypatch.setattr(
        visual.RcloneService,
        "copyto",
        lambda _self, _remote, _remote_path, local_path, timeout=900: Path(local_path).write_bytes(b"master"),
    )
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda _master, output_dir, _duration, _count: write_frames(output_dir, 10),
    )

    inputs = visual.collect_visual_frames(asset, "visual-v2")

    assert inputs.frame_cache_dir == tmp_path / "previews" / str(asset.id) / "visual-v2-frames"


def test_visual_v2_regenerates_separate_cache_from_visual_v1(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "profile-cache")
    session.add(asset)
    session.commit()
    calls = {"rclone": 0}

    def fake_copyto(_self, _remote: str, _remote_path: str, local_path: str, timeout: int = 900):
        calls["rclone"] += 1
        Path(local_path).write_bytes(b"master")

    monkeypatch.setattr(visual.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda _master, output_dir, _duration, _count: write_frames(output_dir, 10),
    )

    visual.collect_visual_frames(asset, "visual-v1")
    visual.collect_visual_frames(asset, "visual-v2")

    assert calls["rclone"] == 2


def test_visual_analysis_persists_structured_json_and_updates_asset(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "apply-result")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, frames: good_visual_result(),
    )

    analysis = latest_analysis(session, asset)
    assert analysis.prompt_version == VISUAL_PROFILE_VERSION
    assert analysis.input_type == "visual_intelligence"
    assert analysis.result_json["status"] == "ready"
    assert analysis.result_json["frame_count"] == 10
    assert analysis.result_json["visual"]["quality"]["score"] == 0.82
    assert analysis.result_json["visual"]["composition"]["subject_trajectory"] == []
    assert analysis.result_json["editorial_policy"]["status"] == "searchable"
    assert asset.quality_score == 0.82
    assert asset.editorial_status == "searchable"
    assert asset.editorial_quality_score == 0.82
    assert asset.vertical_suitability_score == 0.9
    assert asset.horizontal_suitability_score == 0.55
    assert asset.flip_horizontal_allowed is False
    assert asset.crop_allowed is True
    assert asset.zoom_allowed is True
    assert asset.max_safe_zoom == 1.1
    assert asset.enrichment_version is None
    assert asset.visual_description == "Persona trabajando en una mesa con una laptop."
    assert asset.action_description == "trabajar"
    assert asset.location == "oficina"
    assert asset.best_for == "tutoriales"
    assert asset.avoid_for == "deportes"


@pytest.mark.parametrize(
    ("flag", "expected_code"),
    [
        ("subscribe_cta", "subscribe_cta"),
        ("social_media_ui", "social_media_ui"),
        ("emoji_overlay", "emoji_overlay"),
        ("subject_badly_clipped", "subject_badly_clipped"),
        ("subject_severely_out_of_frame", "subject_severely_out_of_frame"),
    ],
)
def test_visual_garbage_hard_rejects(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
    expected_code: str,
) -> None:
    asset = make_asset(source, f"reject-{flag}")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: visual_result_with_garbage(**{flag: True}),
    )

    assert asset.editorial_status == "rejected"
    assert asset.auto_select_enabled is False
    assert expected_code in (asset.editorial_reason_codes or [])


def test_raw_reason_without_structured_signal_does_not_reject(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "reason-only")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: good_visual_result(quality_reason_codes=["invented_reject"]),
    )

    assert asset.editorial_status == "searchable"


def test_watermark_quarantines_and_disables_auto_select(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "watermark")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: visual_result_with_garbage(watermark=True),
    )

    assert asset.editorial_status == "quarantined"
    assert asset.auto_select_enabled is False


def test_clean_usable_is_searchable_without_reactivating_auto_select(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "clean-disabled", auto_select_enabled=False)
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    assert asset.editorial_status == "searchable"
    assert asset.auto_select_enabled is False


def test_transform_policy_blocks_flip_for_text_logo_and_caps_zoom(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "transform-blocks")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: good_visual_result(visible_text="Cafe", logo=True, max_safe_zoom=1.8),
    )

    assert asset.flip_horizontal_allowed is False
    assert "visible_text" in asset.flip_risk_reasons
    assert "logo" in asset.flip_risk_reasons
    assert asset.max_safe_zoom <= 1.10


def test_edge_subject_blocks_zoom(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "edge-zoom")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: good_visual_result(edge_proximity=0.9),
    )

    assert asset.zoom_allowed is False
    assert asset.max_safe_zoom == 1.0


def test_pan_policy_existing_motion_blocks_static_margin_allows(
    session: Session,
    source: Source,
) -> None:
    moving = visual.normalize_visual_transforms(good_visual_result(camera_motion="tracking"), make_asset(source, "moving"))
    static = visual.normalize_visual_transforms(
        good_visual_result(camera_motion="static", negative_space=0.45, edge_proximity=0.2),
        make_asset(source, "static"),
    )

    assert moving.pan_allowed is False
    assert static.pan_allowed is True


def test_crop_vertical_and_horizontal_are_independent(
    source: Source,
) -> None:
    result = good_visual_result(crop_vertical_allowed=True, crop_horizontal_allowed=False)
    transforms = visual.normalize_visual_transforms(result, make_asset(source, "crop-independent"))

    assert transforms.crop_vertical_allowed is True
    assert transforms.crop_horizontal_allowed is False
    assert transforms.crop_allowed is True


def test_visual_status_failed_is_current(
    session: Session,
    source: Source,
) -> None:
    asset = make_asset(source, "failed-current")
    session.add(asset)
    session.flush()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="nvidia",
            input_type="visual_intelligence",
            prompt_version=VISUAL_PROFILE_VERSION,
            result_json={"status": "failed", "error": "rclone failed"},
        )
    )
    session.commit()

    data = visual.visual_intelligence_status(session)

    assert data["pipeline"]["processed"] == 1
    assert data["pipeline"]["failed"] == 1
    assert data["pipeline"]["remaining"] == 1


def test_successful_visual_retry_clears_current_failed_status(
    session: Session,
    source: Source,
) -> None:
    asset = make_asset(source, "retry-success")
    session.add(asset)
    session.flush()
    session.add_all(
        [
            AssetAIAnalysis(
                asset=asset,
                model="test-model",
                provider="nvidia",
                input_type="visual_intelligence",
                prompt_version=VISUAL_PROFILE_VERSION,
                result_json={"status": "failed", "error": "missing frames"},
            ),
            AssetAIAnalysis(
                asset=asset,
                model="test-model",
                provider="nvidia",
                input_type="visual_intelligence",
                prompt_version=VISUAL_PROFILE_VERSION,
                result_json={"status": "ready", "source_fingerprint": visual.source_fingerprint(asset)},
            ),
        ]
    )
    session.commit()

    data = visual.visual_intelligence_status(session)

    assert data["pipeline"]["processed"] == 1
    assert data["pipeline"]["failed"] == 0
    assert data["pipeline"]["remaining"] == 0


def test_reprocess_visual_assets_forces_new_attempt(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "force-reprocess")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    result = visual.reprocess_visual_assets(
        session,
        [asset.id],
        apply=True,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    assert result.selected == 1
    assert result.processed == 1
    assert result.failed == 0
    assert latest_analysis(session, asset).result_json["status"] == "ready"


def test_visual_recalibration_skips_current_visual_v1(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "skip-current")
    session.add(asset)
    session.flush()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="nvidia",
            input_type="visual_intelligence",
            prompt_version="visual-v1",
            result_json={"status": "ready", "source_fingerprint": visual.source_fingerprint(asset)},
        )
    )
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: pytest.fail("visual-v1 should be current"),
    )

    assert len(asset.ai_analyses) == 1


def test_visual_recalibration_visual_v2_is_candidate_and_asset_uid_stable(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "v2-candidate")
    original_uid = asset.asset_uid
    session.add(asset)
    session.flush()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="nvidia",
            input_type="visual_intelligence",
            prompt_version="visual-v1",
            result_json={"status": "ready", "source_fingerprint": visual.source_fingerprint(asset)},
        )
    )
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        profile_version="visual-v2",
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    assert latest_analysis(session, asset).prompt_version == "visual-v2"
    assert asset.asset_uid == original_uid


def test_visual_force_recalculates_current_asset(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "force-current")
    original_uid = asset.asset_uid
    session.add(asset)
    session.flush()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="nvidia",
            input_type="visual_intelligence",
            prompt_version="visual-v1",
            result_json={"status": "ready", "source_fingerprint": visual.source_fingerprint(asset)},
        )
    )
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        force=True,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    assert len(asset.ai_analyses) == 2
    assert asset.asset_uid == original_uid


class FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def make_asset(source: Source, uid: str, auto_select_enabled: bool = True) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_test",
        remote_path=f"videos/{uid}.mp4",
        source_path=f"videos/{uid}.mp4",
        filename=f"{uid}.mp4",
        type="video",
        status="ready",
        move_status="moved",
        source_status="active",
        duration_seconds=11.0,
        orientation="9:16",
        width=1080,
        height=1920,
        auto_select_enabled=auto_select_enabled,
    )


def good_visual_result(
    *,
    visible_text: str | None = None,
    logo: bool = False,
    watermark: bool = False,
    max_safe_zoom: float = 1.2,
    edge_proximity: float = 0.2,
    negative_space: float = 0.35,
    camera_motion: str = "static",
    quality_reason_codes: list[str] | None = None,
    crop_vertical_allowed: bool = True,
    crop_horizontal_allowed: bool = True,
) -> VisualIntelligenceResult:
    return VisualIntelligenceResult.model_validate(
        {
            "quality": {
                "score": 0.82,
                "label": "good",
                "sharpness": 0.8,
                "exposure": 0.84,
                "stability": 0.77,
                "lighting": 0.81,
                "temporal_consistency": 0.86,
                "reason_codes": quality_reason_codes or ["clean_motion"],
            },
            "garbage": {
                "is_garbage": False,
                "score": 0.05,
                "black_or_blank": False,
                "subject_severely_out_of_frame": False,
                "subject_badly_clipped": False,
                "social_media_ui": False,
                "subscribe_cta": False,
                "emoji_overlay": False,
                "watermark": watermark,
                "logo": logo,
                "heavy_text_overlay": False,
                "nearly_empty": False,
                "severe_blur": False,
                "severe_black_frames": False,
                "corrupted_frames": False,
                "accidental_capture": False,
                "editorial_usable": True,
                "reasons": [],
            },
            "semantics": {
                "summary_es": "Persona trabajando en una mesa con una laptop.",
                "subjects": ["persona", "laptop"],
                "actions": ["trabajar"],
                "objects": ["mesa"],
                "emotions": ["calma"],
                "narrative_themes": ["productividad"],
                "possible_use_cases": ["tutoriales"],
                "negative_use_cases": ["deportes"],
                "setting": "oficina",
                "mood": "calmo",
                "keywords_es": ["oficina", "trabajo", "laptop"],
                "contains_people": True,
                "visible_text": visible_text,
                "logo_or_watermark": logo or watermark,
            },
            "composition": {
                "shot_type": "medium",
                "subject_position": "center",
                "subject_framing": "comfortable",
                "subject_region": [0.25, 0.2, 0.5, 0.6],
                "subject_trajectory": [],
                "negative_space": negative_space,
                "edge_proximity": edge_proximity,
                "vertical_suitability": 0.9,
                "horizontal_suitability": 0.55,
                "camera_motion": camera_motion,
                "camera_motion_confidence": 0.9,
                "safe_text_areas": ["top"],
                "crop_risk_reasons": [],
            },
            "transforms": {
                "flip": {"allowed": False, "confidence": 0.9, "risk_reasons": ["handedness"]},
                "zoom": {"allowed": True, "max_safe_zoom": max_safe_zoom, "confidence": 0.9, "risk_reasons": []},
                "pan": {
                    "allowed": True,
                    "safe_directions": ["left", "right"],
                    "max_offset_x": 0.08,
                    "max_offset_y": 0.05,
                    "confidence": 0.9,
                    "risk_reasons": [],
                },
                "crop_vertical": {
                    "allowed": crop_vertical_allowed,
                    "safe_rect": [0.15, 0.0, 0.7, 1.0],
                    "confidence": 0.9,
                    "preferred_aspect_ratios": ["9:16"],
                    "risk_reasons": [],
                },
                "crop_horizontal": {
                    "allowed": crop_horizontal_allowed,
                    "safe_rect": [0.0, 0.2, 1.0, 0.6],
                    "confidence": 0.9,
                    "preferred_aspect_ratios": ["16:9"],
                    "risk_reasons": [],
                },
            },
            "confidence": 0.88,
            "needs_human_review": False,
            "review_reason": None,
        }
    )


def visual_result_with_garbage(**flags: bool) -> VisualIntelligenceResult:
    payload = good_visual_result().model_dump(mode="json")
    for key, value in flags.items():
        payload["garbage"][key] = value
    hard_flags = set(flags) - {"watermark", "heavy_text_overlay"}
    if any(flags[key] for key in hard_flags):
        payload["garbage"]["editorial_usable"] = False
        payload["garbage"]["score"] = 0.9
    return VisualIntelligenceResult.model_validate(payload)


def write_frames(output_dir: Path, count: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(1, count + 1):
        frame = output_dir / f"frame_{index:03d}.jpg"
        frame.write_bytes(b"jpg")
        frames.append(frame)
    return frames


def patch_frame_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    asset_id: int,
) -> None:
    frame_dir = tmp_path / "previews" / str(asset_id) / "visual-v1-frames"
    inputs = visual.VisualFrameInputs(
        master_path=tmp_path / "master.mp4",
        frame_paths=write_frames(frame_dir, 10),
        frame_cache_dir=frame_dir,
        temp_dir=tmp_path / "temp",
    )
    inputs.master_path.write_bytes(b"master")
    monkeypatch.setattr(visual, "collect_visual_frames", lambda _asset, _profile_version="visual-v1": inputs)


def latest_analysis(session: Session, asset: Asset) -> AssetAIAnalysis:
    analysis = session.scalars(
        select(AssetAIAnalysis)
        .where(AssetAIAnalysis.asset_id == asset.id)
        .order_by(AssetAIAnalysis.id.desc())
    ).first()
    assert analysis is not None
    return analysis
