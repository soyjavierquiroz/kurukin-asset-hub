from __future__ import annotations

from base64 import b64encode
from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db_session
from app.main import create_app
from app.models import Asset, Source


def auth_header(username: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def make_test_app():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session() -> Generator[Session, None, None]:
        with session_factory() as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    return app, session_factory


def seed_segmented_asset(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        source = Source(
            source_id="raw-long",
            provider="google_drive",
            label="Raw long",
            rclone_remote="raw",
            source_role="raw_long",
        )
        derived_source = Source(
            source_id="raw-long_derived",
            provider="google_drive",
            label="Derived",
            rclone_remote="derived",
            source_role="derived_broll",
        )
        parent = Asset(
            asset_uid="asset-long-001",
            source=source,
            provider="google_drive",
            rclone_remote="raw",
            remote_path="raw/long.mp4",
            filename="long.mp4",
            type="video",
            duration_seconds=90,
            segmentation_status="ready",
            segment_count=1,
            delete_original_eligible=True,
            auto_select_enabled=False,
        )
        child = Asset(
            asset_uid="asset-long-001-seg-001",
            source=derived_source,
            provider="google_drive",
            rclone_remote="derived",
            remote_path="other/long_segment_001.mp4",
            filename="long_segment_001.mp4",
            title="Long segmento 001",
            type="video",
            duration_seconds=8,
            parent_asset=parent,
            is_derivative=True,
            derivative_type="segment",
            segment_index=1,
            segment_start_seconds=0,
            segment_end_seconds=8,
            preview_status="pending",
            ai_enrichment_status="pending",
        )
        session.add_all([parent, child])
        session.commit()
        return parent.id


def test_asset_detail_shows_segment_video_button() -> None:
    app, session_factory = make_test_app()
    asset_id = seed_segmented_asset(session_factory)
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Segment video" in response.text
    assert "Long video" in response.text


def test_asset_detail_shows_derived_clips() -> None:
    app, session_factory = make_test_app()
    asset_id = seed_segmented_asset(session_factory)
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Derived clips" in response.text
    assert "Long Video Segmentation" in response.text
    assert "long_segment_001.mp4" in response.text
    assert "other/long_segment_001.mp4" in response.text
