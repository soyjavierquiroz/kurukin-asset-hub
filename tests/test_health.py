from collections.abc import Generator

from fastapi.testclient import TestClient

from app.db import get_db_session
from app.main import create_app


class FakeSession:
    def execute(self, statement: object) -> None:
        return None


def override_db_session() -> Generator[FakeSession, None, None]:
    yield FakeSession()


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
