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


def seed_preview_asset(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        source = Source(
            source_id="gdrive_code_x",
            provider="google_drive",
            label="Drive Code X",
            rclone_remote="gdrive_code_x",
            root_path="assets",
        )
        asset = Asset(
            asset_uid="asset_ui_001",
            source=source,
            provider="google_drive",
            rclone_remote="gdrive_code_x",
            remote_path="assets/video.mp4",
            filename="video.mp4",
            type="video",
            preview_status="ready",
            technical_metadata_status="ready",
            thumbnail_path="assets/previews/1/thumbnail.jpg",
            preview_path="assets/previews/1/preview.mp4",
        )
        session.add(asset)
        session.commit()
        return asset.id


def test_asset_detail_shows_generate_preview_button_and_video() -> None:
    app, session_factory = make_test_app()
    asset_id = seed_preview_asset(session_factory)
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Regenerate Preview" in response.text
    assert "/media/assets/previews/1/preview.mp4" in response.text
    assert "<video" in response.text


def test_media_preview_requires_basic_auth() -> None:
    app, _ = make_test_app()
    client = TestClient(app)

    response = client.get("/media/previews/asset_ui_001/thumbnail.jpg")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
