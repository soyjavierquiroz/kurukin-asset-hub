from collections.abc import Generator
from base64 import b64encode

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db_session
from app.main import create_app
from app.models import Brand, Source


class FakeSession:
    def execute(self, statement: object) -> None:
        return None


def override_db_session() -> Generator[FakeSession, None, None]:
    yield FakeSession()


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

    def override_catalog_session() -> Generator[Session, None, None]:
        with session_factory() as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_catalog_session
    return app, session_factory


def test_healthz() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_head() -> None:
    client = TestClient(create_app())

    response = client.head("/healthz")

    assert response.status_code == 200
    assert response.content == b""


def test_readyz() -> None:
    app = create_app()
    app.dependency_overrides[get_db_session] = override_db_session
    client = TestClient(app)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_healthz_stays_public() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200


def test_readyz_stays_public() -> None:
    app = create_app()
    app.dependency_overrides[get_db_session] = override_db_session
    client = TestClient(app)

    response = client.get("/readyz")

    assert response.status_code == 200


def test_dashboard_requires_auth() -> None:
    app, _ = make_test_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_dashboard_with_basic_auth() -> None:
    app, _ = make_test_app()
    client = TestClient(app)

    response = client.get("/", headers=auth_header())

    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_create_brand_via_post() -> None:
    app, session_factory = make_test_app()
    client = TestClient(app)

    response = client.post(
        "/brands",
        data={
            "slug": "kurukin",
            "name": "Kurukin",
            "description": "Brand",
            "visual_line": "Clean",
            "enabled": "on",
        },
        headers=auth_header(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        brand = session.scalar(select(Brand).where(Brand.slug == "kurukin"))
        assert brand is not None
        assert brand.name == "Kurukin"


def test_create_source_via_post() -> None:
    app, session_factory = make_test_app()
    client = TestClient(app)

    response = client.post(
        "/sources",
        data={
            "source_id": "drive-main",
            "provider": "google_drive",
            "label": "Drive Main",
            "rclone_remote": "gdrive",
            "root_path": "/assets",
            "enabled": "on",
        },
        headers=auth_header(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        source = session.scalar(select(Source).where(Source.source_id == "drive-main"))
        assert source is not None
        assert source.label == "Drive Main"
