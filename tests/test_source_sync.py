from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Asset, Source
from app.services.source_sync import build_source_sync_plan, extract_drive_folder_id, scan_source


def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_source(session: Session) -> Source:
    source = Source(
        source_id="drive_grandiosa_mujer_veyra",
        provider="google_drive",
        label="Veyra",
        rclone_remote="drive",
        root_path="Grandiosa/Veyra",
    )
    session.add(source)
    session.flush()
    return source


def remote(
    path: str,
    file_id: str = "file-1",
    size: int = 100,
    modtime: str = "2026-07-20T10:00:00Z",
):
    return {
        "Path": path,
        "Name": path.rsplit("/", 1)[-1],
        "Size": size,
        "ModTime": modtime,
        "ID": file_id,
        "MimeType": "video/mp4",
        "Hashes": {"MD5": "hash-1"},
    }


def make_asset(source: Source, path: str, file_id: str | None = None) -> Asset:
    return Asset(
        asset_uid=f"asset-{path.rsplit('/', 1)[-1]}",
        source=source,
        provider="google_drive",
        rclone_remote="drive",
        remote_path=path,
        filename=path.rsplit("/", 1)[-1],
        file_ext="mp4",
        type="video",
        status="active",
        source_status="active",
        remote_file_id=file_id,
        source_size_bytes=100,
        source_hash="hash-1",
        usage_scope="global",
        auto_select_enabled=True,
    )


def test_extract_drive_folder_id() -> None:
    assert (
        extract_drive_folder_id("https://drive.google.com/drive/folders/folder_123")
        == "folder_123"
    )
    assert extract_drive_folder_id("https://drive.google.com/open?id=folder_456") == "folder_456"
    assert extract_drive_folder_id("folder_7890123") == "folder_7890123"
    assert extract_drive_folder_id("https://example.com/nope") is None


def test_new_asset_detected() -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        plan = build_source_sync_plan(session, source, [remote("clip.mp4")])
    assert len(plan["new"]) == 1


def test_existing_by_remote_file_id() -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        session.add(make_asset(source, "Grandiosa/Veyra/old.mp4", "file-1"))
        session.commit()
        plan = build_source_sync_plan(session, source, [remote("old.mp4", file_id="file-1")])
    assert len(plan["existing"]) == 1


def test_existing_fallback_by_remote_path() -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        session.add(make_asset(source, "Grandiosa/Veyra/old.mp4"))
        session.commit()
        plan = build_source_sync_plan(session, source, [remote("old.mp4", file_id="new-id")])
    assert len(plan["existing"]) == 1


def test_moved_updates_remote_path(monkeypatch) -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        session.add(make_asset(source, "Grandiosa/Veyra/old.mp4", "file-1"))
        session.commit()
        monkeypatch.setattr(
            "app.services.source_sync.RcloneService.list_json",
            lambda *_: [remote("new.mp4")],
        )
        run = scan_source(session, source.id, apply=True)
        asset = session.get(Asset, 1)
    assert run.summary_json["moved"] == 1
    assert asset.remote_path == "Grandiosa/Veyra/new.mp4"


def test_changed_marks_preview_and_ai_pending(monkeypatch) -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        asset = make_asset(source, "Grandiosa/Veyra/clip.mp4", "file-1")
        asset.preview_status = "ready"
        asset.technical_metadata_status = "ready"
        asset.ai_enrichment_status = "ready"
        session.add(asset)
        session.commit()
        monkeypatch.setattr(
            "app.services.source_sync.RcloneService.list_json",
            lambda *_: [remote("clip.mp4", size=200)],
        )
        scan_source(session, source.id, apply=True)
        asset = session.get(Asset, asset.id)
    assert asset.preview_status == "pending"
    assert asset.technical_metadata_status == "pending"
    assert asset.ai_enrichment_status == "pending"


def test_missing_marks_asset_without_delete(monkeypatch) -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        asset = make_asset(source, "Grandiosa/Veyra/clip.mp4", "file-1")
        session.add(asset)
        session.commit()
        monkeypatch.setattr("app.services.source_sync.RcloneService.list_json", lambda *_: [])
        scan_source(session, source.id, apply=True)
        asset = session.get(Asset, asset.id)
    assert asset is not None
    assert asset.source_status == "missing"
    assert asset.source_missing_at is not None


def test_recovered_returns_active(monkeypatch) -> None:
    sf = session_factory()
    with sf() as session:
        source = make_source(session)
        asset = make_asset(source, "Grandiosa/Veyra/clip.mp4", "file-1")
        asset.source_status = "missing"
        session.add(asset)
        session.commit()
        monkeypatch.setattr(
            "app.services.source_sync.RcloneService.list_json",
            lambda *_: [remote("clip.mp4")],
        )
        scan_source(session, source.id, apply=True)
        asset = session.get(Asset, asset.id)
    assert asset.source_status == "active"
    assert asset.source_missing_at is None
