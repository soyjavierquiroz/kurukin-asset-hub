from base64 import b64encode
from collections.abc import Generator
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db_session
from app.main import create_app
from app.models import Asset, AssetAIAnalysis, Source


def auth_header() -> dict[str, str]:
    token = b64encode(b"admin:change-me").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def make_test_app(tmp_path: Path):
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

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    return app, session_factory


def seed_assets(session_factory: sessionmaker[Session], count: int = 3) -> list[int]:
    ids = []
    with session_factory() as session:
        source = Source(
            source_id="managed-drive-pilot",
            provider="google_drive",
            label="Managed Drive Pilot",
            rclone_remote="pilot",
            root_path="",
        )
        session.add(source)
        for index in range(count):
            status = "review_required" if index == 0 else "ready"
            asset = Asset(
                asset_uid=f"pilot-{index}",
                source=source,
                provider="google_drive",
                remote_path=f"source/path-{index}.mp4",
                source_path=f"source/path-{index}.mp4",
                drive_file_id=f"drive-{index}",
                original_parent_id="inbox",
                filename=f"asset-{index}.mp4",
                type="video" if index != 2 else "image",
                status=status,
                scope="generic",
                move_status="not_planned",
                orientation="9:16" if index != 1 else "16:9",
                primary_theme="personas" if index == 0 else "naturaleza",
                primary_topic="yoga" if index == 0 else "bosque",
                thumbnail_path=f"pilot-previews/{index + 1}/thumbnail.webp",
                needs_human_review=index == 0,
                review_reason="classification_ambiguous" if index == 0 else None,
            )
            session.add(asset)
            session.flush()
            session.add(
                AssetAIAnalysis(
                    asset=asset,
                    model="pilot-model",
                    provider="nvidia",
                    input_type="preview",
                    prompt_version="test",
                    confidence=0.42 if index == 0 else 0.9,
                    result_json={
                        "contains_people": index == 0,
                        "people_count": 1 if index == 0 else None,
                        "visual_presentation": "unclear" if index == 0 else "not_applicable",
                        "visual_presentation_confidence": 0.42 if index == 0 else 0.0,
                        "person_visibility": "silhouette" if index == 0 else "not_applicable",
                        "primary_theme": asset.primary_theme,
                        "primary_topic": asset.primary_topic,
                        "tags": ["piloto", asset.primary_topic],
                        "warnings": ["classification_ambiguous"] if index == 0 else [],
                        "ai_provider": "nvidia",
                        "ai_model": "pilot-model",
                    },
                )
            )
            ids.append(asset.id)
        session.commit()
    return ids


def test_assets_gallery_filters_and_pagination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=55)
    client = TestClient(app)

    response = client.get("/assets?q=asset&primary_theme=personas&page=1", headers=auth_header())

    assert response.status_code == 200
    assert "Assets" in response.text
    assert "asset-0.mp4" in response.text
    assert "Pagina 1 de 1" in response.text

    response = client.get("/assets?page=2", headers=auth_header())
    assert response.status_code == 200
    assert "Pagina 2 de 2" in response.text


def test_asset_detail_shows_pilot_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Drive file ID" in response.text
    assert "drive-0" in response.text
    assert "classification_ambiguous" in response.text
    assert "Visual presentation" in response.text


def test_reviews_queue_and_form(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    queue = client.get("/reviews", headers=auth_header())
    form = client.get(f"/reviews/{asset_id}", headers=auth_header())

    assert queue.status_code == 200
    assert "1 assets pendientes" in queue.text
    assert f"/reviews/{asset_id}" in queue.text
    assert form.status_code == 200
    assert "Revision humana" in form.text
    assert "visual_presentation" in form.text


def test_pilot_preview_serving_and_path_traversal(tmp_path: Path, monkeypatch) -> None:
    preview = tmp_path / "1" / "thumbnail.webp"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"webp")
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, _ = make_test_app(tmp_path)
    client = TestClient(app)

    response = client.get("/media/pilot-previews/1/thumbnail.webp", headers=auth_header())
    traversal = client.get("/media/pilot-previews/../1/thumbnail.webp", headers=auth_header())
    missing = client.get("/media/pilot-previews/1/missing.webp", headers=auth_header())

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == b"webp"
    assert traversal.status_code == 404
    assert missing.status_code == 404


def test_human_approval_records_manual_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}",
        headers=auth_header(),
        data={
            "visual_presentation": "feminine",
            "visual_presentation_confidence": "0.98",
            "person_visibility": "clear",
            "people_count": "1",
            "primary_theme": "personas",
            "primary_topic": "yoga humano",
            "tags": "persona, yoga, aprobado",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        manual = session.scalar(
            select(AssetAIAnalysis)
            .where(AssetAIAnalysis.asset_id == asset_id, AssetAIAnalysis.provider == "human")
            .order_by(AssetAIAnalysis.id.desc())
            .limit(1)
        )
        assert asset is not None
        assert asset.status == "move_planned"
        assert asset.move_status == "planned"
        assert asset.needs_human_review is False
        assert asset.review_reason is None
        assert asset.reviewed_by == "admin"
        assert asset.plan_hash
        assert manual is not None
        assert manual.result_json["human_review_overrides_ai"] is True
        assert manual.result_json["visual_presentation"] == "feminine"


def test_web_requests_do_not_call_drive_or_ai(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]

    def fail(*args, **kwargs):
        raise AssertionError("external call should not run during web review/navigation")

    monkeypatch.setattr("app.web.routes.generate_asset_preview", fail)
    monkeypatch.setattr("app.web.routes.enrich_asset_with_ai", fail)
    monkeypatch.setattr("app.web.routes.scan_source", fail)
    client = TestClient(app)

    assert client.get("/assets", headers=auth_header()).status_code == 200
    assert client.get(f"/assets/{asset_id}", headers=auth_header()).status_code == 200
    assert client.get("/reviews", headers=auth_header()).status_code == 200
    assert client.get(f"/reviews/{asset_id}", headers=auth_header()).status_code == 200


def test_web_uses_configured_pilot_database_url(tmp_path: Path, monkeypatch) -> None:
    pilot_url = "postgresql+psycopg://asset_hub_pilot:secret@127.0.0.1:55491/kurukin_asset_hub_pilot"
    monkeypatch.setenv("DATABASE_URL", pilot_url)
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    get_settings.cache_clear()

    assert get_settings().database_url == pilot_url
    assert "kurukin_asset_hub_pilot" in get_settings().database_url
