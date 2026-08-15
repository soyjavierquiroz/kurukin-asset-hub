from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Asset, AssetAIAnalysis, Source
from app.schemas.visual_intelligence import VISUAL_PROFILE_VERSION, VisualIntelligenceResult
from app.services.ai_providers import nvidia_provider
from app.services.ai_providers.nvidia_provider import NvidiaProviderError
from app.services.asset_location import resolve_asset_rclone_location
from app.services import ai_asset_enrichment
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
    assert inputs.contact_sheet_path == inputs.frame_cache_dir / "contact-sheet.jpg"
    assert inputs.contact_sheet_path.is_file()
    manifest = inputs.frame_cache_dir / "manifest.json"
    assert manifest.is_file()


def test_ten_cached_frames_generate_contact_sheet(tmp_path: Path) -> None:
    frame_dir = tmp_path / "frames"
    frames = write_color_frames(frame_dir, color_sequence())

    contact_sheet = visual.ensure_visual_contact_sheet(frames, frame_dir)

    assert contact_sheet == frame_dir / "contact-sheet.jpg"
    with Image.open(contact_sheet) as image:
        assert image.width <= 1800
        assert image.height <= 1800


def test_contact_sheet_contains_all_frames_in_chronological_order_without_labels(tmp_path: Path) -> None:
    frame_dir = tmp_path / "frames"
    colors = color_sequence()
    frames = write_color_frames(frame_dir, colors)

    contact_sheet = visual.ensure_visual_contact_sheet(frames, frame_dir)

    with Image.open(contact_sheet) as image:
        tile_width = image.width // 5
        tile_height = image.height // 2
        for index, expected in enumerate(colors):
            left = (index % 5) * tile_width
            top = (index // 5) * tile_height
            sample_points = [
                (left + tile_width // 2, top + tile_height // 2),
                (left + tile_width // 4, top + tile_height // 4),
                (left + (tile_width * 3) // 4, top + (tile_height * 3) // 4),
            ]
            for point in sample_points:
                assert color_close(image.getpixel(point), expected)


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
    assert first.contact_sheet_path.is_file()
    assert second.contact_sheet_path.is_file()
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
    visual.ensure_visual_contact_sheet(frames, frame_dir)

    def fail_copyto(*_args, **_kwargs):
        raise AssertionError("rclone should not run for a valid frame cache")

    def fail_resolver(_asset):
        raise AssertionError("location resolution should not run for a valid frame cache")

    monkeypatch.setattr(visual.RcloneService, "copyto", fail_copyto)
    monkeypatch.setattr(visual, "resolve_asset_rclone_location", fail_resolver)

    inputs = visual.collect_visual_frames(asset)

    assert len(inputs.frame_paths) == 10
    assert inputs.contact_sheet_path == frame_dir / "contact-sheet.jpg"


def test_collect_visual_frames_creates_missing_contact_sheet_from_cached_frames_only(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    get_settings.cache_clear()
    asset = make_asset(source, "cache-create-contact")
    session.add(asset)
    session.commit()
    frame_dir = visual.visual_frame_cache_dir(asset.id)
    frames = write_frames(frame_dir, 10)
    visual.write_visual_frame_manifest(frame_dir, VISUAL_PROFILE_VERSION, visual.source_fingerprint(asset), 11.0, frames)

    monkeypatch.setattr(
        visual.RcloneService,
        "copyto",
        lambda *_args, **_kwargs: pytest.fail("rclone should not run when frames are cached"),
    )
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda *_args, **_kwargs: pytest.fail("ffmpeg should not run when frames are cached"),
    )

    inputs = visual.collect_visual_frames(asset)

    assert inputs.frame_paths == frames
    assert inputs.contact_sheet_path.is_file()
    assert inputs.sample_timestamps == visual.visual_frame_timestamps(10, 11.0)


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


def test_visual_provider_500_is_operational_not_rejected_and_keeps_asset_uid(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-500")
    original_uid = asset.asset_uid
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    def fail_provider(*_args, **_kwargs):
        raise NvidiaProviderError("EngineCore encountered an issue HTTP 500")

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(visual, "post_chat_completion", fail_provider)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert analysis.result_json["status"] == "failed"
    assert analysis.result_json["error_type"] == "operational_failure"
    assert analyzed.editorial_status != "rejected"
    assert analyzed.asset_uid == original_uid


def test_visual_provider_503_then_success_marks_asset_ready(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-503-retry")
    original_uid = asset.asset_uid
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    calls = {"count": 0}

    def flaky_provider(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise NvidiaProviderError("ResourceExhausted: Worker local total request limit reached HTTP 503")
        return good_visual_result().model_dump_json()

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(visual, "post_chat_completion", flaky_provider)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert calls["count"] == 2
    assert analysis.result_json["status"] == "ready"
    assert analysis.result_json["attempt_count"] == 2
    assert analysis.result_json["final_error"] is None
    assert analyzed.asset_uid == original_uid


def test_visual_provider_timeout_then_success_marks_asset_ready(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-timeout-retry")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    calls = {"count": 0}

    def flaky_provider(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise NvidiaProviderError("read operation timed out")
        return good_visual_result().model_dump_json()

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(visual, "post_chat_completion", flaky_provider)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert calls["count"] == 2
    assert analysis.result_json["status"] == "ready"
    assert analysis.result_json["attempt_count"] == 2


def test_visual_provider_503_exhausted_is_retryable_operational_failure(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-503-exhausted")
    original_uid = asset.asset_uid
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    calls = {"count": 0}

    def fail_provider(*_args, **_kwargs):
        calls["count"] += 1
        raise NvidiaProviderError("ResourceExhausted: Worker local total request limit reached HTTP 503")

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(visual, "post_chat_completion", fail_provider)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert calls["count"] == 4
    assert analysis.result_json["status"] == "failed"
    assert analysis.result_json["error_type"] == "operational_failure"
    assert analysis.result_json["retryable"] is True
    assert analysis.result_json["attempt_count"] == 4
    assert "HTTP 503" in analysis.result_json["final_error"]
    assert analyzed.editorial_status != "rejected"
    assert analyzed.asset_uid == original_uid


def test_visual_validation_error_does_not_retry(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-validation-no-retry")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    calls = {"count": 0}

    def invalid_payload(*_args, **_kwargs):
        calls["count"] += 1
        return "{}"

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: pytest.fail("validation should not retry"))
    monkeypatch.setattr(visual, "post_chat_completion", invalid_payload)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert calls["count"] == 1
    assert analysis.result_json["status"] == "failed"
    assert analysis.result_json["attempt_count"] == 1


def test_visual_provider_401_does_not_retry(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "provider-401-no-retry")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    calls = {"count": 0}

    def auth_failure(*_args, **_kwargs):
        calls["count"] += 1
        raise NvidiaProviderError("HTTP 401 Unauthorized")

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: pytest.fail("401 should not retry"))
    monkeypatch.setattr(visual, "post_chat_completion", auth_failure)

    analyzed = visual.analyze_asset_visual_intelligence(session, asset.id, force=True)

    analysis = latest_analysis(session, analyzed)
    assert calls["count"] == 1
    assert analysis.result_json["status"] == "failed"
    assert analysis.result_json["retryable"] is False
    assert analysis.result_json["attempt_count"] == 1


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
    assert set(analysis.result_json["visual"]) >= {
        "quality",
        "garbage",
        "semantics",
        "composition",
        "transforms",
        "confidence",
    }
    assert analysis.result_json["visual"]["composition"]["subject_trajectory"] == []
    assert analysis.result_json["editorial_policy"]["status"] == "searchable"
    assert analysis.result_json["normalized_transforms"]["flip_horizontal"]["status"] == "unsafe"
    assert analysis.result_json["normalized_transforms"]["zoom"]["status"] == "safe"
    assert analysis.result_json["profile_version"] == VISUAL_PROFILE_VERSION
    assert analysis.result_json["model"] == analysis.model
    assert analysis.result_json["source_fingerprint"] == visual.source_fingerprint(asset)
    assert asset.quality_score == 0.82
    assert asset.editorial_status == "searchable"
    assert asset.auto_select_enabled is True
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
    assert "persona" in (asset.search_text or "")
    assert "trabajar" in (asset.search_text or "")
    assert "mesa" in (asset.search_text or "")
    assert "calma" in (asset.search_text or "")
    assert "productividad" in (asset.search_text or "")
    assert "tutoriales" in (asset.search_text or "")
    assert "deportes" in (asset.search_text or "")


def test_visual_caller_receives_single_contact_sheet_image_path(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "single-contact")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    seen: dict[str, object] = {}

    def fake_visual_caller(prompt: str, image_paths: list[Path]) -> VisualIntelligenceResult:
        seen["prompt"] = prompt
        seen["image_paths"] = image_paths
        return good_visual_result()

    visual.analyze_asset_visual_intelligence(session, asset.id, visual_caller=fake_visual_caller)

    image_paths = seen["image_paths"]
    assert isinstance(image_paths, list)
    assert image_paths == [tmp_path / "previews" / str(asset.id) / "visual-v1-frames" / "contact-sheet.jpg"]
    assert "cuadricula cronologica" in str(seen["prompt"])
    assert "sample timestamps: [" in str(seen["prompt"])
    assert "NO son texto visible en el video" in str(seen["prompt"])


def test_visual_model_is_stored_in_provenance(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "visual-model")
    get_settings.cache_clear()
    asset = make_asset(source, "visual-model-provenance")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    analysis = latest_analysis(session, asset)
    assert analysis.model == "visual-model"
    assert analysis.prompt_version == VISUAL_PROFILE_VERSION
    assert analysis.result_json["model"] == "visual-model"


def test_visual_normalization_warnings_are_stored_in_provenance(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = make_asset(source, "visual-normalization-provenance")
    session.add(asset)
    session.commit()
    patch_frame_collection(monkeypatch, tmp_path, asset.id)
    result = good_visual_result()
    object.__setattr__(
        result,
        "_normalization_warnings",
        ["composition.subject_region_dropped"],
    )

    visual.analyze_asset_visual_intelligence(
        session,
        asset.id,
        visual_caller=lambda _prompt, _frames: result,
    )

    analysis = latest_analysis(session, asset)
    assert analysis.result_json["normalization_warnings"] == ["composition.subject_region_dropped"]


def test_call_nvidia_visual_intelligence_uses_single_image_and_token_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contact_sheet = write_color_frames(tmp_path, [(120, 30, 200)])[0]
    calls: list[dict[str, object]] = []

    def fake_post_chat_completion(
        prompt: str,
        image_paths: list[Path],
        model: str,
        timeout_seconds: float,
        max_tokens: int | None = None,
        **_kwargs: object,
    ) -> str:
        calls.append(
            {
                "prompt": prompt,
                "image_paths": image_paths,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "max_tokens": max_tokens,
            }
        )
        return good_visual_result().model_dump_json()

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "visual-model")
    get_settings.cache_clear()
    monkeypatch.setattr(visual, "post_chat_completion", fake_post_chat_completion)

    result = visual.call_nvidia_visual_intelligence("prompt", [contact_sheet])

    assert isinstance(result, VisualIntelligenceResult)
    assert calls[0]["image_paths"] == [contact_sheet]
    assert calls[0]["model"] == "visual-model"
    assert calls[0]["max_tokens"] == 2400


def test_legacy_enrichment_uses_nvidia_model_not_visual_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = write_color_frames(tmp_path, [(10, 20, 30)])[0]
    captured: dict[str, object] = {}

    def fake_call_nvidia_vision(prompt: str, image_paths: list[Path], model: str | None = None):
        captured["prompt"] = prompt
        captured["image_paths"] = image_paths
        captured["model"] = model
        return good_visual_result()

    monkeypatch.setenv("AI_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_MODEL", "legacy-model")
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "visual-model")
    get_settings.cache_clear()
    monkeypatch.setattr(ai_asset_enrichment, "call_nvidia_vision", fake_call_nvidia_vision)

    ai_asset_enrichment.call_openai_vision("prompt", [image_path])

    assert captured["image_paths"] == [image_path]
    assert captured["model"] == "legacy-model"


def test_legacy_nvidia_post_chat_completion_uses_settings_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = write_color_frames(tmp_path, [(10, 20, 30)])[0]
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(request, timeout: float):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("NVIDIA_MAX_TOKENS", "900")
    monkeypatch.setenv("NVIDIA_MODEL", "legacy-model")
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "visual-model")
    get_settings.cache_clear()
    monkeypatch.setattr(nvidia_provider.urllib.request, "urlopen", fake_urlopen)

    nvidia_provider.post_chat_completion("prompt", [image_path], "legacy-model", timeout_seconds=12.0)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "legacy-model"
    assert payload["max_tokens"] == 900


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
    assert asset.auto_select_enabled is True
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


def test_watermark_quarantines_and_preserves_auto_select(
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
    assert asset.auto_select_enabled is True


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


def test_flip_low_confidence_without_blocker_is_unknown_not_unsafe(source: Source) -> None:
    result = good_visual_result(flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.48)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "asset-29-concept"))

    flip = transforms.flip_horizontal
    assert flip.status == "unknown"
    assert flip.allowed is False
    assert flip.blockers == []
    assert "low_confidence" in flip.reasons


def test_flip_visible_text_is_unsafe_blocker(source: Source) -> None:
    result = good_visual_result(visible_text="Oferta", flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.96)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "text-blocker"))

    flip = transforms.flip_horizontal
    assert flip.status == "unsafe"
    assert flip.allowed is False
    assert "visible_text" in flip.blockers


def test_flip_clean_high_confidence_is_safe(source: Source) -> None:
    result = good_visual_result(flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.96)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "clean-flip"))

    assert transforms.flip_horizontal.status == "safe"
    assert transforms.flip_horizontal.allowed is True


@pytest.mark.parametrize(
    "transform_key",
    ["flip", "zoom", "pan", "crop_vertical", "crop_horizontal"],
)
def test_transform_confidence_is_required(transform_key: str) -> None:
    payload = good_visual_result().model_dump(mode="json")
    del payload["transforms"][transform_key]["confidence"]

    with pytest.raises(ValidationError):
        VisualIntelligenceResult.model_validate(payload)


def test_transform_confidence_zero_is_valid_when_explicit() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["transforms"]["flip"]["confidence"] = 0.0

    result = VisualIntelligenceResult.model_validate(payload)

    assert result.transforms.flip.confidence == 0.0


def test_transform_confidence_is_preserved_exactly(source: Source) -> None:
    result = good_visual_result(flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.83)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "preserve-confidence"))

    assert result.transforms.flip.confidence == 0.83
    assert transforms.flip_horizontal.confidence == 0.83


def test_transform_policy_keeps_existing_confidence_thresholds(source: Source) -> None:
    clean_high = visual.normalize_visual_transforms(
        good_visual_result(flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.85),
        make_asset(source, "clean-high"),
    )
    clean_low = visual.normalize_visual_transforms(
        good_visual_result(flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.40),
        make_asset(source, "clean-low"),
    )
    blocker = visual.normalize_visual_transforms(
        good_visual_result(visible_text="Oferta", flip_allowed=True, flip_risk_reasons=[], flip_confidence=0.90),
        make_asset(source, "blocker"),
    )

    assert clean_high.flip_horizontal.status == "safe"
    assert clean_high.flip_horizontal.confidence == 0.85
    assert clean_low.flip_horizontal.status == "unknown"
    assert clean_low.flip_horizontal.confidence == 0.40
    assert "low_confidence" in clean_low.flip_horizontal.reasons
    assert blocker.flip_horizontal.status == "unsafe"
    assert blocker.flip_horizontal.confidence == 0.90


def test_visual_intelligence_prompt_does_not_anchor_transform_confidence(source: Source) -> None:
    prompt = visual.build_visual_intelligence_prompt(make_asset(source, "prompt-confidence"), [1.0])

    assert '"confidence": 0.0' not in prompt
    assert "Cada transforms.*.confidence es REQUIRED float 0.0..1.0" in prompt
    assert "allowed significa SEGURIDAD/CAPACIDAD" in prompt
    assert "No uses allowed=false porque el transform no hace falta" in prompt
    assert "camara estatica y margen suficiente son evidencia favorable" in prompt


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


def test_zoom_low_confidence_is_unknown_not_unsafe(source: Source) -> None:
    result = good_visual_result(zoom_allowed=True, zoom_confidence=0.49)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-unknown"))

    assert transforms.zoom.status == "unknown"
    assert transforms.zoom.allowed is False
    assert transforms.zoom.blockers == []
    assert "low_confidence" in transforms.zoom.reasons


def test_zoom_not_needed_is_not_a_real_blocker(source: Source) -> None:
    result = good_visual_result(
        zoom_allowed=False,
        max_safe_zoom=1.05,
        zoom_risk_reasons=["no necesita zoom"],
        zoom_confidence=0.92,
    )

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-not-needed"))

    assert transforms.zoom.status == "unknown"
    assert transforms.zoom.allowed is False
    assert transforms.zoom.blockers == []
    assert "no_necesita_zoom" not in transforms.zoom.reasons
    assert "transforms.zoom.allowed_false_with_safe_zoom" in transforms.consistency_warnings


def test_zoom_clipping_blocker_is_unsafe(source: Source) -> None:
    payload = good_visual_result(zoom_allowed=True).model_dump(mode="json")
    payload["garbage"]["subject_badly_clipped"] = True
    result = VisualIntelligenceResult.model_validate(payload)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-clipped"))

    assert transforms.zoom.status == "unsafe"
    assert transforms.zoom.allowed is False
    assert "subject_badly_clipped" in transforms.zoom.blockers


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
    assert moving.pan.status == "unsafe"
    assert static.pan_allowed is True
    assert static.pan.status == "safe"


def test_pan_static_camera_sufficient_margin_high_confidence_is_safe(source: Source) -> None:
    result = good_visual_result(
        camera_motion="static",
        camera_motion_confidence=0.95,
        negative_space=0.5,
        edge_proximity=0.1,
        pan_allowed=True,
        pan_safe_directions=["left", "right"],
        pan_confidence=0.94,
    )

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "pan-static-margin"))

    assert transforms.pan.status == "safe"
    assert transforms.pan.allowed is True
    assert transforms.pan.safe_directions == ["left", "right"]
    assert transforms.pan.max_offset_x > 0


def test_pan_insufficient_margin_blocker_is_unsafe(source: Source) -> None:
    result = good_visual_result(edge_proximity=0.7, negative_space=0.1, pan_allowed=True)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "pan-no-margin"))

    assert transforms.pan.status == "unsafe"
    assert transforms.pan.allowed is False
    assert "insufficient_composition_margin" in transforms.pan.blockers


def test_pan_low_confidence_is_unknown_not_unsafe(source: Source) -> None:
    result = good_visual_result(pan_confidence=0.51, camera_motion_confidence=0.51)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "pan-unknown"))

    assert transforms.pan.status == "unknown"
    assert transforms.pan.allowed is False
    assert transforms.pan.blockers == []
    assert "low_confidence" in transforms.pan.reasons


def test_crop_vertical_and_horizontal_are_independent(
    source: Source,
) -> None:
    result = good_visual_result(crop_vertical_allowed=True, crop_horizontal_allowed=False)
    transforms = visual.normalize_visual_transforms(result, make_asset(source, "crop-independent"))

    assert transforms.crop_vertical_allowed is True
    assert transforms.crop_horizontal_allowed is False
    assert transforms.crop_allowed is True


def test_crop_low_confidence_is_unknown_and_known_cut_is_unsafe(source: Source) -> None:
    unknown = visual.normalize_visual_transforms(
        good_visual_result(crop_vertical_confidence=0.50),
        make_asset(source, "crop-unknown"),
    )
    unsafe = visual.normalize_visual_transforms(
        good_visual_result(crop_vertical_risk_reasons=["cuts_subject"]),
        make_asset(source, "crop-unsafe"),
    )

    assert unknown.crop_vertical.status == "unknown"
    assert unknown.crop_vertical.allowed is False
    assert unknown.crop_vertical.blockers == []
    assert "low_confidence" in unknown.crop_vertical.reasons
    assert unsafe.crop_vertical.status == "unsafe"
    assert unsafe.crop_vertical.allowed is False
    assert "cuts_subject" in unsafe.crop_vertical.blockers


def test_flip_clean_high_confidence_without_directionality_is_safe(source: Source) -> None:
    result = good_visual_result(flip_allowed=True, flip_confidence=0.93, flip_risk_reasons=[])

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "flip-safe"))

    assert transforms.flip_horizontal.status == "safe"
    assert transforms.flip_horizontal.allowed is True


def test_flip_asymmetric_hands_alone_is_not_unsafe(source: Source) -> None:
    result = good_visual_result(
        flip_allowed=False,
        flip_confidence=0.93,
        flip_risk_reasons=["manos asimetricas"],
    )

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "flip-hands-only"))

    assert transforms.flip_horizontal.status == "unknown"
    assert transforms.flip_horizontal.allowed is False
    assert transforms.flip_horizontal.blockers == []


def test_flip_high_confidence_false_empty_risks_is_unknown_with_warning(source: Source) -> None:
    result = good_visual_result(flip_allowed=False, flip_confidence=0.93, flip_risk_reasons=[])

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "flip-empty-risk"))

    assert transforms.flip_horizontal.status == "unknown"
    assert transforms.flip_horizontal.allowed is False
    assert "transforms.flip.allowed_false_high_confidence_empty_risk_reasons" in transforms.consistency_warnings


def test_crop_allowed_true_without_safe_rect_is_unknown_with_warning(source: Source) -> None:
    result = good_visual_result(crop_vertical_allowed=True, crop_vertical_safe_rect_null=True)

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "crop-null-rect"))

    assert transforms.crop_vertical.status == "unknown"
    assert transforms.crop_vertical.allowed is False
    assert transforms.crop_vertical.safe_rect is None
    assert "geometry_unavailable" in transforms.crop_vertical.reasons
    assert "transforms.crop_vertical.allowed_true_null_safe_rect" in transforms.consistency_warnings


def test_crop_allowed_true_with_valid_safe_rect_high_confidence_is_safe(source: Source) -> None:
    result = good_visual_result(
        crop_vertical_allowed=True,
        crop_vertical_safe_rect=[0.1, 0.0, 0.8, 1.0],
        crop_vertical_confidence=0.93,
    )

    transforms = visual.normalize_visual_transforms(result, make_asset(source, "crop-valid-rect"))

    assert transforms.crop_vertical.status == "safe"
    assert transforms.crop_vertical.allowed is True
    assert transforms.crop_vertical.safe_rect == [0.1, 0.0, 0.8, 1.0]


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "test-model")
    get_settings.cache_clear()
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
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "test-model")
    get_settings.cache_clear()
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


def test_visual_model_change_reprocesses_without_regenerating_cached_frames(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "previews"))
    monkeypatch.setenv("NVIDIA_VISUAL_MODEL", "new-visual-model")
    get_settings.cache_clear()
    asset = make_asset(source, "model-change-cache")
    original_uid = asset.asset_uid
    session.add(asset)
    session.flush()
    frame_dir = visual.visual_frame_cache_dir(asset.id)
    frames = write_frames(frame_dir, 10)
    visual.write_visual_frame_manifest(frame_dir, VISUAL_PROFILE_VERSION, visual.source_fingerprint(asset), 11.0, frames)
    visual.ensure_visual_contact_sheet(frames, frame_dir)
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="old-visual-model",
            provider="nvidia",
            input_type="visual_intelligence",
            prompt_version=VISUAL_PROFILE_VERSION,
            result_json={"status": "ready", "source_fingerprint": visual.source_fingerprint(asset)},
        )
    )
    session.commit()

    monkeypatch.setattr(
        visual.RcloneService,
        "copyto",
        lambda *_args, **_kwargs: pytest.fail("rclone should not run for cached frames"),
    )
    monkeypatch.setattr(
        visual,
        "extract_temporal_frames",
        lambda *_args, **_kwargs: pytest.fail("ffmpeg should not run for cached frames"),
    )

    result = visual.reprocess_visual_assets(
        session,
        [asset.id],
        apply=True,
        visual_caller=lambda _prompt, _frames: good_visual_result(),
    )

    analysis = latest_analysis(session, asset)
    assert result.processed == 1
    assert result.failed == 0
    assert analysis.model == "new-visual-model"
    assert analysis.result_json["frame_cache_path"] == str(frame_dir)
    assert len(asset.ai_analyses) == 2
    assert asset.asset_uid == original_uid


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


def test_visual_payload_normalizes_subject_region_string_to_null() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["composition"]["subject_region"] = "centro"

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)

    assert result.composition.subject_region is None
    assert warnings == ["composition.subject_region_dropped"]


def test_visual_payload_normalizes_subject_trajectory_strings_to_empty() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["composition"]["subject_trajectory"] = ["caminar", "girar"]

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)

    assert result.composition.subject_trajectory == []
    assert warnings == ["composition.subject_trajectory_invalid_items_dropped"]


def test_visual_payload_keeps_only_valid_mixed_trajectory_points() -> None:
    valid_point = {"timestamp": 1.25, "center": [0.5, 0.45], "bbox": [0.2, 0.1, 0.5, 0.8]}
    payload = good_visual_result().model_dump(mode="json")
    payload["composition"]["subject_trajectory"] = [
        valid_point,
        "stationary",
        {"timestamp": -1, "center": [0.4], "bbox": "center"},
    ]

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)

    assert len(result.composition.subject_trajectory) == 1
    assert result.composition.subject_trajectory[0].timestamp == valid_point["timestamp"]
    assert result.composition.subject_trajectory[0].center == valid_point["center"]
    assert result.composition.subject_trajectory[0].bbox == valid_point["bbox"]
    assert warnings == ["composition.subject_trajectory_invalid_items_dropped"]


def test_visual_payload_normalizes_optional_transform_geometry() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["transforms"]["crop_vertical"]["safe_rect"] = "center"
    payload["transforms"]["crop_horizontal"]["safe_rect"] = {"x": 0.2}
    payload["transforms"]["crop"] = {
        "allowed": True,
        "safe_rect": ["left", 0.0, 1.0, 1.0],
        "confidence": 0.8,
        "preferred_aspect_ratios": ["1:1"],
        "risk_reasons": [],
    }
    payload["transforms"]["pan"]["max_offset_x"] = "unknown"
    payload["transforms"]["pan"]["max_offset_y"] = 0.2

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)

    assert result.transforms.crop_vertical.safe_rect is None
    assert result.transforms.crop_horizontal.safe_rect is None
    assert result.transforms.crop is not None
    assert result.transforms.crop.safe_rect is None
    assert result.transforms.pan.max_offset_x is None
    assert result.transforms.pan.max_offset_y == 0.2
    assert warnings == [
        "transforms.pan.max_offset_x_dropped",
        "transforms.crop_vertical.safe_rect_dropped",
        "transforms.crop_horizontal.safe_rect_dropped",
        "transforms.crop.safe_rect_dropped",
    ]


def test_visual_payload_normalizes_zoom_identity_when_disallowed(source: Source) -> None:
    payload = good_visual_result(zoom_allowed=False).model_dump(mode="json")
    payload["transforms"]["zoom"]["max_safe_zoom"] = 0.0
    payload["transforms"]["zoom"]["confidence"] = 0.88

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)
    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-disallowed-identity"))

    assert result.transforms.zoom.max_safe_zoom == 1.0
    assert result.transforms.zoom.confidence == 0.88
    assert transforms.zoom.status == "unknown"
    assert transforms.zoom.allowed is False
    assert transforms.zoom.max_safe_zoom == 1.0
    assert warnings == ["transforms.zoom.max_safe_zoom_below_identity_normalized"]


def test_visual_payload_normalizes_zoom_identity_to_unknown_when_allowed(source: Source) -> None:
    payload = good_visual_result(zoom_allowed=True).model_dump(mode="json")
    payload["transforms"]["zoom"]["max_safe_zoom"] = 0.0

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)
    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-allowed-identity"))

    assert result.transforms.zoom.max_safe_zoom == 1.0
    assert transforms.zoom.status == "unknown"
    assert transforms.zoom.allowed is False
    assert transforms.zoom.max_safe_zoom == 1.0
    assert warnings == ["transforms.zoom.max_safe_zoom_below_identity_normalized"]
    assert "transforms.zoom.allowed_true_without_safe_zoom" in transforms.consistency_warnings


def test_visual_payload_keeps_valid_small_safe_zoom(source: Source) -> None:
    payload = good_visual_result(zoom_allowed=True, max_safe_zoom=1.05).model_dump(mode="json")

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)
    transforms = visual.normalize_visual_transforms(result, make_asset(source, "zoom-small-safe"))

    assert result.transforms.zoom.max_safe_zoom == 1.05
    assert transforms.zoom.status == "safe"
    assert transforms.zoom.allowed is True
    assert transforms.zoom.max_safe_zoom == 1.05
    assert warnings == []


def test_visual_payload_normalizes_nonnumeric_zoom_to_identity() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["transforms"]["zoom"]["max_safe_zoom"] = "unsafe"
    payload["transforms"]["zoom"]["confidence"] = 0.81

    normalized, warnings = visual.normalize_visual_payload(payload)
    result = VisualIntelligenceResult.model_validate(normalized)

    assert result.transforms.zoom.max_safe_zoom == 1.0
    assert result.transforms.zoom.confidence == 0.81
    assert warnings == ["transforms.zoom.max_safe_zoom_invalid_normalized"]


def test_visual_payload_core_malformed_quality_score_still_fails() -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["quality"]["score"] = "excellent"

    normalized, warnings = visual.normalize_visual_payload(payload)

    assert warnings == []
    with pytest.raises(Exception):
        VisualIntelligenceResult.model_validate(normalized)


def test_call_nvidia_visual_intelligence_records_normalization_warnings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = good_visual_result().model_dump(mode="json")
    payload["composition"]["subject_region"] = "centro"
    payload["composition"]["subject_trajectory"] = ["caminar", "girar"]
    payload["transforms"]["crop_vertical"]["safe_rect"] = "center"
    payload["transforms"]["pan"]["max_offset_x"] = "unknown"

    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    get_settings.cache_clear()
    monkeypatch.setattr(visual, "post_chat_completion", lambda *_args, **_kwargs: json.dumps(payload))

    result = visual.call_nvidia_visual_intelligence("prompt", [tmp_path / "contact-sheet.jpg"])

    assert result.composition.subject_region is None
    assert result.composition.subject_trajectory == []
    assert result.transforms.crop_vertical.safe_rect is None
    assert result.transforms.pan.max_offset_x is None
    assert getattr(result, "_normalization_warnings") == [
        "composition.subject_region_dropped",
        "composition.subject_trajectory_invalid_items_dropped",
        "transforms.pan.max_offset_x_dropped",
        "transforms.crop_vertical.safe_rect_dropped",
    ]


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
    camera_motion_confidence: float = 0.9,
    quality_reason_codes: list[str] | None = None,
    flip_allowed: bool = False,
    flip_confidence: float = 0.9,
    flip_risk_reasons: list[str] | None = None,
    zoom_allowed: bool = True,
    zoom_confidence: float = 0.9,
    zoom_risk_reasons: list[str] | None = None,
    pan_allowed: bool = True,
    pan_safe_directions: list[str] | None = None,
    pan_confidence: float = 0.9,
    crop_vertical_allowed: bool = True,
    crop_horizontal_allowed: bool = True,
    crop_vertical_safe_rect: list[float] | None = None,
    crop_horizontal_safe_rect: list[float] | None = None,
    crop_vertical_safe_rect_null: bool = False,
    crop_horizontal_safe_rect_null: bool = False,
    crop_vertical_confidence: float = 0.9,
    crop_horizontal_confidence: float = 0.9,
    crop_vertical_risk_reasons: list[str] | None = None,
    crop_horizontal_risk_reasons: list[str] | None = None,
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
                "camera_motion_confidence": camera_motion_confidence,
                "safe_text_areas": ["top"],
                "crop_risk_reasons": [],
            },
            "transforms": {
                "flip": {
                    "allowed": flip_allowed,
                    "confidence": flip_confidence,
                    "risk_reasons": ["handedness"] if flip_risk_reasons is None else flip_risk_reasons,
                },
                "zoom": {
                    "allowed": zoom_allowed,
                    "max_safe_zoom": max_safe_zoom,
                    "confidence": zoom_confidence,
                    "risk_reasons": zoom_risk_reasons or [],
                },
                "pan": {
                    "allowed": pan_allowed,
                    "safe_directions": ["left", "right"] if pan_safe_directions is None else pan_safe_directions,
                    "max_offset_x": 0.08,
                    "max_offset_y": 0.05,
                    "confidence": pan_confidence,
                    "risk_reasons": [],
                },
                "crop_vertical": {
                    "allowed": crop_vertical_allowed,
                    "safe_rect": (
                        None
                        if crop_vertical_safe_rect_null
                        else crop_vertical_safe_rect or [0.15, 0.0, 0.7, 1.0]
                    ),
                    "confidence": crop_vertical_confidence,
                    "preferred_aspect_ratios": ["9:16"],
                    "risk_reasons": crop_vertical_risk_reasons or [],
                },
                "crop_horizontal": {
                    "allowed": crop_horizontal_allowed,
                    "safe_rect": (
                        None
                        if crop_horizontal_safe_rect_null
                        else crop_horizontal_safe_rect or [0.0, 0.2, 1.0, 0.6]
                    ),
                    "confidence": crop_horizontal_confidence,
                    "preferred_aspect_ratios": ["16:9"],
                    "risk_reasons": crop_horizontal_risk_reasons or [],
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
    colors = [
        ((index * 23) % 256, (index * 47) % 256, (index * 71) % 256)
        for index in range(1, count + 1)
    ]
    return write_color_frames(output_dir, colors)


def write_color_frames(output_dir: Path, colors: list[tuple[int, int, int]]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, color in enumerate(colors, start=1):
        frame = output_dir / f"frame_{index:03d}.jpg"
        Image.new("RGB", (40, 40), color).save(frame, format="JPEG", quality=100)
        frames.append(frame)
    return frames


def color_sequence() -> list[tuple[int, int, int]]:
    return [
        (220, 20, 60),
        (255, 140, 0),
        (255, 215, 0),
        (50, 205, 50),
        (0, 128, 255),
        (75, 0, 130),
        (199, 21, 133),
        (0, 206, 209),
        (128, 128, 128),
        (255, 255, 255),
    ]


def color_close(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(channel - target) <= 6 for channel, target in zip(actual, expected, strict=True))


def patch_frame_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    asset_id: int,
) -> None:
    frame_dir = tmp_path / "previews" / str(asset_id) / "visual-v1-frames"
    frames = write_frames(frame_dir, 10)
    visual.write_visual_frame_manifest(frame_dir, VISUAL_PROFILE_VERSION, "test-fingerprint", 11.0, frames)
    contact_sheet = visual.ensure_visual_contact_sheet(frames, frame_dir)
    inputs = visual.VisualFrameInputs(
        master_path=tmp_path / "master.mp4",
        frame_paths=frames,
        contact_sheet_path=contact_sheet,
        frame_cache_dir=frame_dir,
        temp_dir=tmp_path / "temp",
        sample_timestamps=visual.visual_frame_timestamps(10, 11.0),
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
