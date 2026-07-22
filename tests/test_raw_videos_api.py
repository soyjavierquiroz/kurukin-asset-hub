from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db_session
from app.main import create_app
from app.models import RawVideo

API_HEADERS = {"X-Asset-Hub-Api-Key": "test-api-key"}


def make_test_client() -> tuple[TestClient, sessionmaker[Session]]:
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
    return TestClient(app), session_factory


def test_raw_videos_readonly_api_exposes_status_on_both_prefixes() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        session.add(
            RawVideo(
                remote_path="video.mp4",
                provider="gdrive_people_raw_long",
                status="DONE",
                segments_generated=2,
                processed_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        session.commit()

    direct_response = client.get("/raw-videos/status", headers=API_HEADERS)
    api_response = client.get("/api/raw-videos/status", headers=API_HEADERS)

    assert direct_response.status_code == 200
    assert api_response.status_code == 200
    assert direct_response.json()["DONE"] == 1
    assert api_response.json()["DONE"] == 1
