from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Asset, AssetAIAnalysis, Source
from app.schemas.editorial_quality import QUALITY_PROFILE_VERSION, EditorialQualityVLMResult
from app.services import editorial_quality as quality
from app.services.ai_asset_enrichment import AssetImageInputs


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def source(session: Session) -> Source:
    source = Source(source_id="managed-drive-pilot-test", provider="google_drive", label="Managed Drive Pilot")
    session.add(source)
    session.commit()
    return source


def test_editorial_migration_adds_pending_default() -> None:
    migration = runpy.run_path("alembic/versions/202608150001_editorial_quality_gate.py")

    assert migration["revision"] == "202608150001"
    assert migration["down_revision"] == "202608040002"
    assert "pending" in migration["EDITORIAL_STATUS_VALUES"]


def test_new_asset_defaults_to_pending(session: Session, source: Source) -> None:
    asset = make_asset(source, "fresh")
    session.add(asset)
    session.commit()

    assert asset.editorial_status == "pending"


def test_vlm_json_validation_rejects_unknown_fields() -> None:
    payload = good_payload()
    payload["extra"] = True

    with pytest.raises(ValueError):
        EditorialQualityVLMResult.model_validate(payload)


def test_hard_reject_disables_auto_select(session: Session, source: Source, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset(source, "bad")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(
            good_payload(subject_severely_out_of_frame=True, confidence=0.91)
        ),
    )

    assert asset.editorial_status == "rejected"
    assert asset.auto_select_enabled is False
    assert "subject_severely_out_of_frame" in asset.editorial_reason_codes


def test_quarantine_disables_auto_select(session: Session, source: Source, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset(source, "watermark")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(
            good_payload(watermark=True, confidence=0.86)
        ),
    )

    assert asset.editorial_status == "quarantined"
    assert asset.auto_select_enabled is False


def test_searchable_keeps_auto_select_state(session: Session, source: Source, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset(source, "good", auto_select_enabled=False)
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )

    assert asset.editorial_status == "searchable"
    assert asset.auto_select_enabled is False


def test_corrupt_asset_is_rejected_without_vlm(session: Session, source: Source) -> None:
    asset = make_asset(source, "corrupt")
    asset.technical_metadata_status = "failed"
    asset.probe_error = "ffprobe failed"
    session.add(asset)
    session.commit()

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: pytest.fail("VLM should not be called"),
    )

    assert asset.editorial_status == "rejected"
    assert asset.auto_select_enabled is False
    assert "content_decode_impossible" in asset.editorial_reason_codes


def test_preview_transient_failure_stays_pending(session: Session, source: Source) -> None:
    asset = make_asset(source, "preview-transient")
    asset.preview_status = "failed"
    uid_before = asset.asset_uid
    session.add(asset)
    session.commit()

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: pytest.fail("VLM should not be called"),
    )

    assert asset.asset_uid == uid_before
    assert asset.editorial_status == "pending"
    assert asset.auto_select_enabled is True
    assert asset.quality_profile_version == QUALITY_PROFILE_VERSION
    analysis = latest_analysis(session, asset)
    assert analysis.result_json["error"]
    assert analysis.result_json["error_type"] == "operational_failure"


def test_vlm_provider_failure_stays_pending(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "vlm-failure")
    uid_before = asset.asset_uid
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: (_ for _ in ()).throw(RuntimeError("NVIDIA timeout")),
    )

    assert asset.asset_uid == uid_before
    assert asset.editorial_status == "pending"
    assert asset.auto_select_enabled is True
    analysis = latest_analysis(session, asset)
    assert analysis.result_json["decision"] == "pending"
    assert "timeout" in analysis.result_json["error"].lower()


def test_operational_failure_can_retry_to_searchable(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "retry-searchable")
    uid_before = asset.asset_uid
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    assert asset.editorial_status == "pending"

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )

    assert asset.asset_uid == uid_before
    assert asset.editorial_status == "searchable"
    assert asset.auto_select_enabled is True


def test_missing_visual_sample_can_retry_when_pilot_thumbnail_appears(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    get_settings.cache_clear()
    asset = make_asset(source, "retry-missing-sample")
    asset.thumbnail_path = "pilot-previews/28/thumbnail.webp"
    session.add(asset)
    session.commit()

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: pytest.fail("VLM should not run without an image"),
    )
    failed_analysis = latest_analysis(session, asset)
    assert asset.editorial_status == "pending"
    assert "missing visual sample" in failed_analysis.result_json["error"]

    thumbnail = tmp_path / "28" / "thumbnail.webp"
    thumbnail.parent.mkdir(parents=True)
    thumbnail.write_bytes(b"webp")

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )

    assert asset.editorial_status == "searchable"
    assert asset.quality_profile_version == QUALITY_PROFILE_VERSION
    assert latest_analysis(session, asset).id != failed_analysis.id


def test_operational_failure_can_retry_to_quarantine(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "retry-quarantine")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(
            good_payload(watermark=True)
        ),
    )

    assert asset.editorial_status == "quarantined"
    assert asset.auto_select_enabled is False


def test_operational_failure_can_retry_to_rejected(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "retry-rejected")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(
            good_payload(subscribe_cta=True, confidence=0.9)
        ),
    )

    assert asset.editorial_status == "rejected"
    assert asset.auto_select_enabled is False


def test_quality_v1_idempotence_force_and_new_version(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "idempotent")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)
    calls = {"count": 0}

    def caller(*_args):
        calls["count"] += 1
        return EditorialQualityVLMResult.model_validate(good_payload())

    quality.analyze_asset_editorial_quality(session, asset.id, vision_caller=caller)
    quality.analyze_asset_editorial_quality(session, asset.id, vision_caller=caller)
    assert calls["count"] == 1

    quality.analyze_asset_editorial_quality(session, asset.id, force=True, vision_caller=caller)
    assert calls["count"] == 2

    quality.analyze_asset_editorial_quality(session, asset.id, profile_version="quality-v2", vision_caller=caller)
    assert calls["count"] == 3
    assert asset.quality_profile_version == "quality-v2"


def test_backfill_does_not_loop_on_operational_failure(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = make_asset(source, "one-failure")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)
    calls = {"count": 0}

    def failing_caller(*_args):
        calls["count"] += 1
        raise RuntimeError("provider down")

    result = quality.run_editorial_quality_backfill(
        session,
        batch_size=1,
        apply=True,
        vision_caller=failing_caller,
    )

    assert calls["count"] == 1
    assert result.failed == 1
    assert result.remaining == 1
    assert asset.editorial_status == "pending"


def test_backfill_resumes_after_processed_assets(
    session: Session,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_asset(source, "first")
    second = make_asset(source, "second")
    session.add_all([first, second])
    session.commit()
    patch_inputs(monkeypatch)

    result = quality.run_editorial_quality_backfill(
        session,
        limit=1,
        batch_size=1,
        apply=True,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )
    assert result.processed == 1
    assert result.remaining == 1

    result = quality.run_editorial_quality_backfill(
        session,
        batch_size=1,
        apply=True,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )
    assert result.processed == 1
    assert result.remaining == 0


def test_status_counts_pipeline(session: Session, source: Source) -> None:
    searchable = make_asset(source, "searchable")
    searchable.editorial_status = "searchable"
    searchable.quality_profile_version = QUALITY_PROFILE_VERSION
    rejected = make_asset(source, "rejected")
    rejected.editorial_status = "rejected"
    rejected.quality_profile_version = QUALITY_PROFILE_VERSION
    pending = make_asset(source, "pending")
    session.add_all([searchable, rejected, pending])
    session.commit()

    data = quality.editorial_quality_status(session)

    assert data["total"] == 3
    assert data["pending"] == 1
    assert data["searchable"] == 1
    assert data["rejected"] == 1
    assert data["pipeline"]["processed"] == 2
    assert data["pipeline"]["remaining"] == 1


def test_status_ignores_historical_failure_after_successful_retry(session: Session, source: Source) -> None:
    asset = make_asset(source, "failed-then-success")
    asset.editorial_status = "searchable"
    asset.quality_profile_version = QUALITY_PROFILE_VERSION
    session.add(asset)
    session.flush()
    session.add_all(
        [
            AssetAIAnalysis(
                asset=asset,
                model="test-model",
                provider="nvidia",
                input_type="editorial_quality",
                prompt_version=QUALITY_PROFILE_VERSION,
                result_json={
                    "decision": "pending",
                    "error": "missing visual sample",
                    "error_type": "operational_failure",
                },
            ),
            AssetAIAnalysis(
                asset=asset,
                model="test-model",
                provider="nvidia",
                input_type="editorial_quality",
                prompt_version=QUALITY_PROFILE_VERSION,
                result_json={"decision": "searchable", "error": None},
            ),
        ]
    )
    session.commit()

    data = quality.editorial_quality_status(session)

    assert data["pipeline"]["failed"] == 0
    assert data["pipeline"]["processed"] == 1


def test_status_counts_current_pending_operational_failure(session: Session, source: Source) -> None:
    asset = make_asset(source, "current-failure")
    asset.editorial_status = "pending"
    asset.quality_profile_version = QUALITY_PROFILE_VERSION
    session.add(asset)
    session.flush()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="nvidia",
            input_type="editorial_quality",
            prompt_version=QUALITY_PROFILE_VERSION,
            result_json={
                "decision": "pending",
                "error": "missing visual sample",
                "error_type": "operational_failure",
            },
        )
    )
    session.commit()

    data = quality.editorial_quality_status(session)

    assert data["pipeline"]["failed"] == 1
    assert data["pipeline"]["processed"] == 0
    assert data["pipeline"]["remaining"] == 1


def test_analysis_provenance_is_stored(session: Session, source: Source, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset(source, "provenance")
    session.add(asset)
    session.commit()
    patch_inputs(monkeypatch)

    quality.analyze_asset_editorial_quality(
        session,
        asset.id,
        vision_caller=lambda *_args: EditorialQualityVLMResult.model_validate(good_payload()),
    )

    analysis = session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset.id))
    assert analysis is not None
    assert analysis.input_type == "editorial_quality"
    assert analysis.prompt_version == QUALITY_PROFILE_VERSION
    assert analysis.result_json["quality_profile_version"] == QUALITY_PROFILE_VERSION
    assert analysis.result_json["source_fingerprint"]


def make_asset(source: Source, uid: str, auto_select_enabled: bool = True) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        remote_path=f"20_genericos/video/{uid}.mp4",
        source_path=f"20_genericos/video/{uid}.mp4",
        drive_file_id=f"file-{uid}",
        remote_file_id=f"file-{uid}",
        filename=f"{uid}.mp4",
        status="ready",
        move_status="moved",
        source_status="active",
        scope="generic",
        type="video",
        mime_type="video/mp4",
        width=1080,
        height=1920,
        duration_seconds=8.0,
        orientation="9:16",
        preview_status="ready",
        technical_metadata_status="ready",
        auto_select_enabled=auto_select_enabled,
    )


def good_payload(**overrides):
    payload = {
        "editorial_quality_score": 0.88,
        "vertical_suitability_score": 0.91,
        "horizontal_suitability_score": 0.62,
        "subject_severely_out_of_frame": False,
        "subject_badly_clipped": False,
        "social_media_ui": False,
        "subscribe_cta": False,
        "emoji_overlay": False,
        "watermark": False,
        "heavy_text_overlay": False,
        "nearly_empty": False,
        "severe_blur": False,
        "severe_black_frames": False,
        "editorial_usable": True,
        "confidence": 0.9,
        "reason_codes": [],
    }
    payload.update(overrides)
    return payload


def patch_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        quality,
        "collect_asset_images",
        lambda _asset: AssetImageInputs(image_paths=[Path("/tmp/frame.jpg")], input_type="preview"),
    )


def latest_analysis(session: Session, asset: Asset) -> AssetAIAnalysis:
    analysis = session.scalars(
        select(AssetAIAnalysis)
        .where(AssetAIAnalysis.asset_id == asset.id)
        .order_by(AssetAIAnalysis.id.desc())
    ).first()
    assert analysis is not None
    return analysis
