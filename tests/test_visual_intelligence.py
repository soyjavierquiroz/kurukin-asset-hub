from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Asset, AssetAIAnalysis, Source
from app.schemas.visual_intelligence import VISUAL_PROFILE_VERSION, VisualIntelligenceResult
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

    assert inputs.master_path.is_file()
    assert inputs.frame_cache_dir == tmp_path / "previews" / str(asset.id) / "visual-v1-frames"
    assert len(inputs.frame_paths) == 10


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
    assert asset.quality_score == 0.82
    assert asset.flip_horizontal_allowed is False
    assert asset.crop_allowed is True
    assert asset.zoom_allowed is True
    assert asset.max_safe_zoom == 1.2
    assert asset.enrichment_version == VISUAL_PROFILE_VERSION


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


class FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def make_asset(source: Source, uid: str) -> Asset:
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
    )


def good_visual_result() -> VisualIntelligenceResult:
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
                "reason_codes": ["clean_motion"],
            },
            "garbage": {
                "is_garbage": False,
                "score": 0.05,
                "black_or_blank": False,
                "severe_blur": False,
                "corrupted_frames": False,
                "accidental_capture": False,
                "reasons": [],
            },
            "semantics": {
                "summary_es": "Persona trabajando en una mesa con una laptop.",
                "subjects": ["persona", "laptop"],
                "actions": ["trabajar"],
                "setting": "oficina",
                "mood": "calmo",
                "keywords_es": ["oficina", "trabajo", "laptop"],
                "contains_people": True,
                "visible_text": None,
                "logo_or_watermark": False,
            },
            "composition": {
                "shot_type": "medium",
                "subject_position": "center",
                "subject_framing": "comfortable",
                "vertical_suitability": 0.9,
                "horizontal_suitability": 0.55,
                "safe_text_areas": ["top"],
                "crop_risk_reasons": [],
            },
            "transforms": {
                "flip": {"allowed": False, "risk_reasons": ["handedness"]},
                "zoom": {"allowed": True, "max_safe_zoom": 1.2, "risk_reasons": []},
                "pan": {"allowed": True, "safe_directions": ["left", "right"], "risk_reasons": []},
                "crop": {"allowed": True, "preferred_aspect_ratios": ["9:16"], "risk_reasons": []},
            },
            "confidence": 0.88,
            "needs_human_review": False,
            "review_reason": None,
        }
    )


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
