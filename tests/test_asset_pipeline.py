from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base
from app.models import Asset, Source
from app.services import asset_pipeline
from app.services.asset_pipeline import pending_pipeline_asset_ids, process_pending_assets


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


def make_asset(session: Session) -> Asset:
    source = Source(
        source_id="drive_pipeline",
        provider="google_drive",
        label="Pipeline source",
        rclone_remote="drive",
        root_path="assets",
    )
    asset = Asset(
        asset_uid="asset_pipeline_001",
        source=source,
        provider="google_drive",
        rclone_remote="drive",
        remote_path="assets/clip.mp4",
        filename="clip.mp4",
        type="video",
        status="active",
        source_status="active",
        preview_status="pending",
        ai_enrichment_status="pending",
    )
    session.add(asset)
    session.commit()
    return asset


def test_pipeline_generates_preview_then_runs_ai(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PREVIEW_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    asset = make_asset(session)
    calls: list[str] = []

    def fake_generate_preview(db_session: Session, asset_id: int, force: bool = False) -> Asset:
        calls.append(f"preview:{force}")
        db_asset = db_session.get(Asset, asset_id)
        assert db_asset is not None
        output_dir = tmp_path / "assets" / "previews" / str(asset_id)
        output_dir.mkdir(parents=True)
        (output_dir / "thumbnail.jpg").write_bytes(b"thumbnail")
        (output_dir / "preview.mp4").write_bytes(b"preview")
        db_asset.thumbnail_path = f"assets/previews/{asset_id}/thumbnail.jpg"
        db_asset.preview_path = f"assets/previews/{asset_id}/preview.mp4"
        db_asset.preview_status = "ready"
        db_session.commit()
        return db_asset

    def fake_enrich_asset(db_session: Session, asset_id: int) -> Asset:
        calls.append("ai")
        db_asset = db_session.get(Asset, asset_id)
        assert db_asset is not None
        db_asset.ai_enrichment_status = "ready"
        db_session.commit()
        return db_asset

    monkeypatch.setattr(asset_pipeline, "generate_asset_preview", fake_generate_preview)
    monkeypatch.setattr(asset_pipeline, "enrich_asset_with_ai", fake_enrich_asset)

    summary = process_pending_assets(session, limit=5)

    assert calls == ["preview:True", "ai"]
    assert summary.selected == 1
    assert summary.previews_ready == 1
    assert summary.ai_ready == 1
    refreshed = session.get(Asset, asset.id)
    assert refreshed is not None
    assert refreshed.preview_status == "ready"
    assert refreshed.ai_enrichment_status == "ready"


def test_pending_pipeline_selects_recent_pending_assets_only(session: Session) -> None:
    source = Source(
        source_id="drive_pipeline_recent",
        provider="google_drive",
        label="Pipeline source",
        rclone_remote="drive",
        root_path="assets",
    )
    now = datetime.now(UTC)
    historical_ready = Asset(
        asset_uid="historical_ready",
        source=source,
        provider="google_drive",
        rclone_remote="drive",
        remote_path="assets/historico.mp4",
        filename="historico.mp4",
        type="video",
        status="active",
        source_status="active",
        preview_status="ready",
        ai_enrichment_status="ready",
        last_indexed_at=now - timedelta(days=30),
    )
    historical_failed = Asset(
        asset_uid="historical_failed",
        source=source,
        provider="google_drive",
        rclone_remote="drive",
        remote_path="assets/historico_fallido.mp4",
        filename="historico_fallido.mp4",
        type="video",
        status="active",
        source_status="active",
        preview_status="failed",
        ai_enrichment_status="failed",
        last_indexed_at=now - timedelta(days=20),
    )
    recent_pending = Asset(
        asset_uid="recent_pending",
        source=source,
        provider="google_drive",
        rclone_remote="drive",
        remote_path="assets/reciente.mp4",
        filename="reciente.mp4",
        type="video",
        status="active",
        source_status="active",
        preview_status="pending",
        ai_enrichment_status="pending",
        last_indexed_at=now,
    )
    session.add_all([historical_ready, historical_failed, recent_pending])
    session.commit()

    assert pending_pipeline_asset_ids(session, limit=10) == [recent_pending.id]


def test_recent_preview_pending_asset_runs_ai_even_if_previous_ai_was_skipped(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PREVIEW_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    asset = make_asset(session)
    asset.ai_enrichment_status = "skipped"
    session.commit()
    calls: list[str] = []

    def fake_generate_preview(db_session: Session, asset_id: int, force: bool = False) -> Asset:
        calls.append("preview")
        db_asset = db_session.get(Asset, asset_id)
        assert db_asset is not None
        output_dir = tmp_path / "assets" / "previews" / str(asset_id)
        output_dir.mkdir(parents=True)
        (output_dir / "thumbnail.jpg").write_bytes(b"thumbnail")
        (output_dir / "preview.mp4").write_bytes(b"preview")
        db_asset.thumbnail_path = f"assets/previews/{asset_id}/thumbnail.jpg"
        db_asset.preview_path = f"assets/previews/{asset_id}/preview.mp4"
        db_asset.preview_status = "ready"
        db_session.commit()
        return db_asset

    def fake_enrich_asset(db_session: Session, asset_id: int) -> Asset:
        calls.append("ai")
        db_asset = db_session.get(Asset, asset_id)
        assert db_asset is not None
        db_asset.ai_enrichment_status = "ready"
        db_session.commit()
        return db_asset

    monkeypatch.setattr(asset_pipeline, "generate_asset_preview", fake_generate_preview)
    monkeypatch.setattr(asset_pipeline, "enrich_asset_with_ai", fake_enrich_asset)

    summary = process_pending_assets(session, limit=5)

    assert calls == ["preview", "ai"]
    assert summary.previews_ready == 1
    assert summary.ai_ready == 1

