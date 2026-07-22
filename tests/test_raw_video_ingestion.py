from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, RawVideo, Source
from app.services import raw_video_ingestion as svc
from scripts.backfill_raw_videos import main as backfill_raw_videos_main


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()


def seed_source(session: Session) -> Source:
    source = Source(
        source_id="gdrive_people_raw_long",
        provider="google_drive",
        label="People raw long",
        rclone_remote="gdrive_people_raw_long",
        root_path="",
        source_role="raw_long",
    )
    session.add(source)
    session.commit()
    return source


def seed_parent_asset(session: Session, remote_path: str = "video.mp4") -> Asset:
    source = seed_source(session)
    asset = Asset(
        asset_uid=f"asset-{remote_path}",
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_people_raw_long",
        remote_path=remote_path,
        filename=remote_path.rsplit("/", 1)[-1],
        type="video",
        duration_seconds=90.0,
        is_derivative=False,
        source_status="active",
    )
    session.add(asset)
    session.commit()
    return asset


class FakeRclone:
    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    def list_json(self, remote: str, root: str) -> list[dict]:
        return self.entries


def test_sync_raw_videos_detects_new_and_avoids_duplicates() -> None:
    session = make_session()
    rclone = FakeRclone(
        [
            {"Path": "video-a.mp4", "Size": 100},
            {"Path": "notes.txt", "Size": 10},
        ]
    )

    first = svc.sync_raw_videos(session, rclone=rclone)
    second = svc.sync_raw_videos(session, rclone=rclone)
    raw_videos = session.scalars(select(RawVideo)).all()

    assert first.videos_found == 1
    assert first.videos_new == 1
    assert second.videos_found == 1
    assert second.videos_existing == 1
    assert len(raw_videos) == 1
    assert raw_videos[0].remote_path == "video-a.mp4"


def test_claim_raw_video_is_atomic_by_status() -> None:
    session = make_session()
    raw_video = RawVideo(
        remote_path="video.mp4",
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="NEW",
        last_seen_at=datetime.now(UTC),
    )
    session.add(raw_video)
    session.commit()

    first = svc.claim_raw_video(session, raw_video.id, expected_status="NEW")
    second = svc.claim_raw_video(session, raw_video.id, expected_status="NEW")

    assert first is not None
    assert first.status == "PROCESSING"
    assert second is None


def test_process_raw_video_transitions_new_to_done(monkeypatch) -> None:
    session = make_session()
    parent = seed_parent_asset(session)
    raw_video = RawVideo(
        remote_path=parent.remote_path,
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="NEW",
        last_seen_at=datetime.now(UTC),
    )
    session.add(raw_video)
    session.commit()

    monkeypatch.setattr(
        svc,
        "segment_long_video_asset",
        lambda **kwargs: SimpleNamespace(id=77, report_json={"children_created": 3}),
    )

    summary = svc.process_raw_videos(session, limit=5)

    assert summary.videos_processed == 1
    assert summary.segments_generated == 3
    assert raw_video.status == "DONE"
    assert raw_video.segments_generated == 3
    assert raw_video.processed_at is not None


def test_process_raw_video_transitions_new_to_failed(monkeypatch) -> None:
    session = make_session()
    parent = seed_parent_asset(session)
    raw_video = RawVideo(
        remote_path=parent.remote_path,
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="NEW",
        last_seen_at=datetime.now(UTC),
    )
    session.add(raw_video)
    session.commit()

    def fail(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "segment_long_video_asset", fail)

    summary = svc.process_raw_videos(session, limit=5)

    assert summary.videos_failed == 1
    assert raw_video.status == "FAILED"
    assert raw_video.processed_at is None
    assert "boom" in (raw_video.last_error or "")


def test_retry_failed_processes_only_failed(monkeypatch) -> None:
    session = make_session()
    parent = seed_parent_asset(session, "failed.mp4")
    failed = RawVideo(
        remote_path=parent.remote_path,
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="FAILED",
        last_seen_at=datetime.now(UTC),
    )
    new = RawVideo(
        remote_path="new.mp4",
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="NEW",
        last_seen_at=datetime.now(UTC),
    )
    session.add_all([failed, new])
    session.commit()
    monkeypatch.setattr(
        svc,
        "segment_long_video_asset",
        lambda **kwargs: SimpleNamespace(id=1, report_json={"children_created": 1}),
    )

    summary = svc.process_raw_videos(session, status="FAILED")

    assert summary.videos_processed == 1
    assert failed.status == "DONE"
    assert new.status == "NEW"


def test_resume_marks_processing_as_failed(monkeypatch) -> None:
    session = make_session()
    raw_video = RawVideo(
        remote_path="stuck.mp4",
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="PROCESSING",
        last_seen_at=datetime.now(UTC),
    )
    session.add(raw_video)
    session.commit()
    monkeypatch.setattr(
        svc,
        "segment_long_video_asset",
        lambda **kwargs: SimpleNamespace(id=1, report_json={"children_created": 1}),
    )

    summary = svc.process_raw_videos(session, resume=True)

    assert summary.videos_found == 0
    assert raw_video.status == "FAILED"
    assert raw_video.last_error == "Interrupted processing"


def test_backfill_raw_videos_groups_by_source_video(monkeypatch, capsys) -> None:
    session = make_session()
    source = seed_source(session)
    assets = [
        Asset(
            asset_uid="child-1",
            source=source,
            provider="google_drive",
            remote_path="couples/a.mp4",
            source_video="original.mp4",
            filename="a.mp4",
            type="video",
            is_derivative=True,
        ),
        Asset(
            asset_uid="child-2",
            source=source,
            provider="google_drive",
            remote_path="couples/b.mp4",
            source_video="original.mp4",
            filename="b.mp4",
            type="video",
            is_derivative=True,
        ),
    ]
    session.add_all(assets)
    session.commit()
    monkeypatch.setattr("scripts.backfill_raw_videos.SessionLocal", lambda: session)
    monkeypatch.setattr("sys.argv", ["backfill_raw_videos.py"])

    assert backfill_raw_videos_main() == 0

    output = capsys.readouterr().out
    raw_video = session.scalar(select(RawVideo).where(RawVideo.remote_path == "original.mp4"))
    assert '"raw_videos_created": 1' in output
    assert raw_video is not None
    assert raw_video.status == "DONE"
    assert raw_video.segments_generated == 2
    assert {asset.raw_video_id for asset in assets} == {raw_video.id}


def test_raw_video_status_counts_include_empty_statuses() -> None:
    session = make_session()
    raw_video = RawVideo(
        remote_path="done.mp4",
        provider=svc.DEFAULT_RAW_VIDEO_PROVIDER,
        status="DONE",
        last_seen_at=datetime.now(UTC),
    )
    session.add(raw_video)
    session.commit()

    counts = svc.raw_video_status_counts(session)

    assert counts == {"NEW": 0, "PROCESSING": 0, "DONE": 1, "FAILED": 0}
