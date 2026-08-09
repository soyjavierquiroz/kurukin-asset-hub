from base64 import b64encode
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db_session
from app.main import create_app
from app.models import Asset, AssetAIAnalysis, AssetTag, Source
from app.services.managed_drive_pilot import DriveFile
from app.web.routes import bool_filter


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
                title=f"Asset piloto {index}",
                type="video" if index != 2 else "image",
                status=status,
                scope="generic",
                move_status="not_planned",
                orientation="9:16" if index != 1 else "16:9",
                primary_theme="personas" if index == 0 else "naturaleza",
                primary_topic="yoga" if index == 0 else "bosque",
                description="descripcion rio azul" if index == 1 else "clip generico",
                search_text=f"asset piloto buscable {index}",
                duration_seconds=float(index),
                thumbnail_path=f"pilot-previews/{index + 1}/thumbnail.webp",
                needs_human_review=index == 0,
                review_reason="classification_ambiguous" if index == 0 else None,
                reviewed_at=datetime.now(UTC) - timedelta(days=index) if index > 0 else None,
            )
            session.add(asset)
            session.flush()
            session.add(AssetTag(asset=asset, tag=f"selva-tag-{index}"))
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


class FakeDiscardDrive:
    def __init__(
        self,
        *,
        expected_file_id: str = "drive-0",
        trash_fails: bool = False,
        verify_trashed: bool = True,
    ) -> None:
        self.expected_file_id = expected_file_id
        self.trash_fails = trash_fails
        self.verify_trashed = verify_trashed
        self.trash_calls: list[str] = []
        self.untrash_calls: list[str] = []
        self.get_calls: list[str] = []

    def trash_file(self, file_id: str) -> DriveFile:
        assert file_id == self.expected_file_id
        self.trash_calls.append(file_id)
        if self.trash_fails:
            raise RuntimeError("drive unavailable")
        return self._file(file_id, trashed=True)

    def untrash_file(self, file_id: str) -> DriveFile:
        assert file_id == self.expected_file_id
        self.untrash_calls.append(file_id)
        return self._file(file_id, trashed=False)

    def get_file_metadata(self, file_id: str) -> DriveFile:
        assert file_id == self.expected_file_id
        self.get_calls.append(file_id)
        return self._file(file_id, trashed=self.verify_trashed)

    def _file(self, file_id: str, *, trashed: bool) -> DriveFile:
        return DriveFile(
            id=file_id,
            name="Asset desde Drive.mp4",
            mime_type="video/mp4",
            parents=["inbox"],
            trashed=trashed,
        )


def install_fake_discard_drive(monkeypatch, drive: FakeDiscardDrive) -> FakeDiscardDrive:
    monkeypatch.setattr("app.web.routes.drive_client_from_environment", lambda: drive)
    monkeypatch.setattr("app.web.routes.mutation_client_for_apply", lambda client: client)
    return drive


def mark_pending(session_factory: sessionmaker[Session], asset_id: int, reason: str = "manual_check") -> None:
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.status = "review_required"
        asset.needs_human_review = True
        asset.review_reason = reason
        session.commit()


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


def test_assets_search_by_filename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    response = client.get("/assets?q=asset-2", headers=auth_header())

    assert response.status_code == 200
    assert "asset-2.mp4" in response.text
    assert "asset-0.mp4" not in response.text


def test_assets_search_by_description_topic_and_tags(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    description = client.get("/assets?q=rio azul", headers=auth_header())
    topic = client.get("/assets?q=yoga", headers=auth_header())
    tag = client.get("/assets?q=selva-tag-2", headers=auth_header())

    assert description.status_code == 200
    assert "asset-1.mp4" in description.text
    assert topic.status_code == 200
    assert "asset-0.mp4" in topic.text
    assert tag.status_code == 200
    assert "asset-2.mp4" in tag.text


def test_assets_combined_filters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=4)
    client = TestClient(app)

    response = client.get(
        "/assets?status=ready&media_type=image&orientation=9:16&move_status=not_planned",
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert "asset-2.mp4" in response.text
    assert "asset-1.mp4" not in response.text


def test_assets_sort_options(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    response = client.get("/assets?sort=duration", headers=auth_header())

    assert response.status_code == 200
    assert response.text.index("asset-2.mp4") < response.text.index("asset-1.mp4")


def test_assets_pagination_preserves_parameters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=55)
    client = TestClient(app)

    response = client.get("/assets?q=asset&media_type=video&sort=filename", headers=auth_header())

    assert response.status_code == 200
    assert "q=asset" in response.text
    assert "media_type=video" in response.text
    assert "sort=filename" in response.text
    assert "page=2" in response.text


def test_assets_boolean_filters_accept_empty_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    needs_human_review = client.get("/assets?needs_human_review=", headers=auth_header())
    combined_empty = client.get("/assets?contains_people=&needs_human_review=", headers=auth_header())
    auto_select_empty = client.get("/assets?auto_select_enabled=", headers=auth_header())

    assert needs_human_review.status_code == 200
    assert combined_empty.status_code == 200
    assert auto_select_empty.status_code == 200


def test_assets_problematic_empty_filter_url_returns_html(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    response = client.get(
        "/assets?q=yoga&status=&move_status=&scope=&media_type=&primary_theme=&orientation="
        "&contains_people=&visual_presentation=&person_visibility=&needs_human_review=&sort=newest",
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Assets" in response.text
    assert "bool_parsing" not in response.text


def test_assets_needs_human_review_filters_true_false_and_legacy_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    true_response = client.get("/assets?needs_human_review=true", headers=auth_header())
    false_response = client.get("/assets?needs_human_review=false", headers=auth_header())
    legacy_response = client.get("/assets?needs_review=true", headers=auth_header())

    assert true_response.status_code == 200
    assert "asset-0.mp4" in true_response.text
    assert "asset-1.mp4" not in true_response.text
    assert false_response.status_code == 200
    assert "asset-1.mp4" in false_response.text
    assert "asset-0.mp4" not in false_response.text
    assert legacy_response.status_code == 200
    assert "asset-0.mp4" in legacy_response.text
    assert "asset-1.mp4" not in legacy_response.text


def test_assets_needs_human_review_takes_precedence_over_legacy_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    response = client.get("/assets?needs_review=false&needs_human_review=true", headers=auth_header())

    assert response.status_code == 200
    assert "asset-0.mp4" in response.text
    assert "asset-1.mp4" not in response.text


def test_assets_contains_people_filters_true_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    true_response = client.get("/assets?contains_people=true", headers=auth_header())
    false_response = client.get("/assets?contains_people=false", headers=auth_header())

    assert true_response.status_code == 200
    assert "asset-0.mp4" in true_response.text
    assert "asset-1.mp4" not in true_response.text
    assert false_response.status_code == 200
    assert "asset-1.mp4" in false_response.text
    assert "asset-0.mp4" not in false_response.text


def test_assets_boolean_pagination_preserves_active_true_false_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=55)
    client = TestClient(app)

    response = client.get(
        "/assets?contains_people=false&needs_human_review=false&sort=filename",
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert "contains_people=false" in response.text
    assert "needs_human_review=false" in response.text
    assert "sort=filename" in response.text
    assert "page=2" in response.text


def test_bool_filter_strips_string_values() -> None:
    assert bool_filter(" true ") is True
    assert bool_filter(" FALSE ") is False
    assert bool_filter("  ") is None


def test_assets_empty_result_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory, count=3)
    client = TestClient(app)

    response = client.get("/assets?q=no-existe", headers=auth_header())

    assert response.status_code == 200
    assert "No hay coincidencias" in response.text
    assert "0 de 3 assets" in response.text


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
    assert "Guardar y siguiente" in form.text
    assert "Descartar" in form.text
    assert "Descartar y siguiente" in form.text
    assert "confirm(" in form.text
    assert "asset-0.mp4" in form.text


def test_review_discard_success_trashes_drive_deletes_asset_and_redirects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    preview_dir = tmp_path / str(asset_id)
    preview_dir.mkdir(parents=True)
    (preview_dir / "thumbnail.webp").write_bytes(b"webp")
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/reviews"
    assert drive.trash_calls == ["drive-0"]
    assert drive.get_calls == ["drive-0"]
    with session_factory() as session:
        assert session.get(Asset, asset_id) is None
        assert session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset_id)) is None
    assert not preview_dir.exists()


def test_review_discard_next_redirects_to_next_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    ids = seed_assets(session_factory, count=3)
    mark_pending(session_factory, ids[1])
    client = TestClient(app)

    response = client.post(
        f"/reviews/{ids[0]}/discard",
        headers=auth_header(),
        data={"action": "discard_next"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/reviews/{ids[1]}"
    assert drive.trash_calls == ["drive-0"]
    with session_factory() as session:
        assert session.get(Asset, ids[0]) is None


def test_review_discard_next_completes_when_last_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory, count=1)[0]
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard_next"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/reviews?completed=true"


def test_review_discard_drive_trash_failure_keeps_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(
        monkeypatch,
        FakeDiscardDrive(expected_file_id="drive-0", trash_fails=True),
    )
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 502
    assert drive.trash_calls == ["drive-0"]
    with session_factory() as session:
        assert session.get(Asset, asset_id) is not None


def test_review_discard_drive_verification_failure_keeps_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(
        monkeypatch,
        FakeDiscardDrive(expected_file_id="drive-0", verify_trashed=False),
    )
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 502
    assert drive.trash_calls == ["drive-0"]
    with session_factory() as session:
        assert session.get(Asset, asset_id) is not None


def test_review_discard_db_commit_failure_attempts_untrash_and_keeps_asset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    original_commit = Session.commit
    fail_next_commit = {"enabled": True}

    def fail_commit_once(self):
        if fail_next_commit["enabled"]:
            fail_next_commit["enabled"] = False
            raise RuntimeError("database commit failed")
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", fail_commit_once)
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 500
    assert drive.trash_calls == ["drive-0"]
    assert drive.untrash_calls == ["drive-0"]
    with session_factory() as session:
        assert session.get(Asset, asset_id) is not None


def test_review_discard_never_uses_filename_or_path_for_drive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    drive = install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.filename = "not-the-drive-id.mp4"
        asset.remote_path = "ambiguous/not-the-drive-id.mp4"
        session.commit()
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert drive.trash_calls == ["drive-0"]


def test_discarded_asset_detail_is_404_and_absent_from_assets_and_reviews(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    discard = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )
    detail = client.get(f"/assets/{asset_id}", headers=auth_header())
    assets = client.get("/assets", headers=auth_header())
    reviews = client.get("/reviews", headers=auth_header())

    assert discard.status_code == 303
    assert detail.status_code == 404
    assert "asset-0.mp4" not in assets.text
    assert "asset-0.mp4" not in reviews.text


def test_review_discard_preview_traversal_is_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path / "root"))
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "thumbnail.webp"
    outside_file.write_bytes(b"do-not-delete")
    install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.thumbnail_path = "pilot-previews/../outside/thumbnail.webp"
        asset.preview_path = None
        session.commit()
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}/discard",
        headers=auth_header(),
        data={"action": "discard"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert outside_file.read_bytes() == b"do-not-delete"


def test_review_discard_preview_cleanup_failure_warns_but_succeeds(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    install_fake_discard_drive(monkeypatch, FakeDiscardDrive(expected_file_id="drive-0"))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]

    def fail_cleanup(asset_id: int, preview_paths: list[str | None]) -> None:
        raise RuntimeError("cannot clean preview")

    monkeypatch.setattr("app.web.routes.cleanup_discard_preview_dir", fail_cleanup)
    client = TestClient(app)

    with caplog.at_level("WARNING", logger="app.web.routes"):
        response = client.post(
            f"/reviews/{asset_id}/discard",
            headers=auth_header(),
            data={"action": "discard"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Preview cleanup failed" in caplog.text
    with session_factory() as session:
        assert session.get(Asset, asset_id) is None


def test_reviews_index_does_not_call_drive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    seed_assets(session_factory)

    def fail_drive():
        raise AssertionError("Drive should not be called by GET /reviews")

    monkeypatch.setattr("app.web.routes.drive_client_from_environment", fail_drive)
    client = TestClient(app)

    response = client.get("/reviews", headers=auth_header())

    assert response.status_code == 200


def test_review_save_and_next_redirects_to_next_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    ids = seed_assets(session_factory, count=3)
    with session_factory() as session:
        second = session.get(Asset, ids[1])
        assert second is not None
        second.status = "review_required"
        second.needs_human_review = True
        second.review_reason = "manual_check"
        session.commit()
    client = TestClient(app)

    response = client.post(
        f"/reviews/{ids[0]}",
        headers=auth_header(),
        data={
            "visual_presentation": "feminine",
            "visual_presentation_confidence": "0.98",
            "person_visibility": "clear",
            "people_count": "1",
            "primary_theme": "personas",
            "primary_topic": "yoga humano",
            "tags": "persona, yoga, aprobado",
            "action": "save_next",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/reviews/{ids[1]}"


def test_review_save_and_next_completes_when_none_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory, count=1)[0]
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
            "action": "save_next",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/reviews?completed=true"


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


def test_review_without_meaningful_naming_source_stays_in_review(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.title = None
        asset.description = "Respuesta NVIDIA invalida para source.mp4."
        asset.primary_topic = "revision manual"
        asset.target_name = "someone-is-driving-a-car__16x9__12345678.mp4"
        asset.filename = "source.mp4"
        analysis = asset.ai_analyses[0]
        analysis.result_json = {
            "primary_topic": "revision manual",
            "title_es": "source.mp4",
            "description_es": "Respuesta NVIDIA invalida para source.mp4.",
            "tags": ["manejando", "automovil", "chofer"],
        }
        session.commit()
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}",
        headers=auth_header(),
        data={
            "visual_presentation": "not_applicable",
            "visual_presentation_confidence": "0.4",
            "person_visibility": "not_applicable",
            "people_count": "",
            "primary_theme": "otros",
            "primary_topic": "revision manual",
            "tags": "manejando, automovil, chofer",
        },
    )

    assert response.status_code == 200
    assert "Indica un Primary topic descriptivo para poder aprobar este asset." in response.text
    assert "Primary topic debe ser una frase descriptiva." in response.text
    assert "manejando, automovil, chofer" in response.text
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        assert asset.status == "review_required"
        assert asset.needs_human_review is True
        assert session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset_id, AssetAIAnalysis.provider == "human")) is None


def test_review_with_valid_primary_topic_is_approved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]
    client = TestClient(app)

    response = client.post(
        f"/reviews/{asset_id}",
        headers=auth_header(),
        data={
            "visual_presentation": "not_applicable",
            "visual_presentation_confidence": "0.9",
            "person_visibility": "not_applicable",
            "people_count": "",
            "primary_theme": "personas",
            "primary_topic": "persona haciendo yoga en sala",
            "tags": "persona, yoga",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        assert asset.status == "move_planned"
        assert asset.needs_human_review is False
        assert asset.target_name == "persona-haciendo-yoga-en-sala__9:16__a0754b46.mp4"


def test_edit_metadata_planned_recalculates_plan_without_drive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[1]
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.status = "move_planned"
        asset.move_status = "planned"
        asset.drive_file_id = "drive-planned"
        asset.original_name = "old.mp4"
        asset.original_parent_id = "inbox"
        asset.orientation = "horizontal-16x9"
        asset.target_name = "old__16x9__7a6fdb97.mp4"
        asset.remote_path = f"10_genericos/video/lote-0001/{asset.target_name}"
        asset.source_path = asset.remote_path
        asset.path_layout_version = "compact_v2"
        asset.reviewed_at = datetime.now(UTC)
        old_hash = asset.plan_hash = "old-hash"
        session.commit()

    def fail(*_args, **_kwargs):
        raise AssertionError("Drive or AI must not be called")

    monkeypatch.setattr("app.services.managed_drive_pilot.drive_client_from_environment", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_nvidia_vision", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_openai_vision", fail)
    client = TestClient(app)

    response = client.post(
        f"/assets/{asset_id}/edit-metadata",
        headers=auth_header(),
        data={
            "visual_presentation": "not_applicable",
            "visual_presentation_confidence": "0.9",
            "person_visibility": "not_applicable",
            "people_count": "",
            "primary_theme": "transporte",
            "primary_topic": "conducción urbana en primera persona",
            "tags": "conduccion, ciudad",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/assets/{asset_id}?metadata_status=planned_recalculated"
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        assert asset.move_status == "planned"
        assert asset.status == "move_planned"
        assert asset.target_name == "conduccion-urbana-en-primera-persona__16x9__10259aa3.mp4"
        assert asset.plan_hash and asset.plan_hash != old_hash
        assert session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset_id, AssetAIAnalysis.provider == "nvidia")) is not None
        assert session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset_id, AssetAIAnalysis.provider == "human")) is not None

    detail = client.get(response.headers["location"], headers=auth_header())
    assert "Metadata actualizada · plan recalculado" in detail.text


def test_edit_metadata_moved_does_not_call_drive_or_change_move_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[1]
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.status = "ready"
        asset.move_status = "moved"
        asset.drive_file_id = "drive-moved"
        asset.original_name = "old.mp4"
        asset.original_parent_id = "inbox"
        asset.target_parent_id = "parent"
        asset.filename = "old__16x9__c38b9647.mp4"
        asset.target_name = asset.filename
        asset.remote_path = f"10_genericos/video/lote-0001/{asset.target_name}"
        asset.source_path = asset.remote_path
        asset.path_layout_version = "compact_v2"
        session.commit()

    def fail(*_args, **_kwargs):
        raise AssertionError("Drive or AI must not be called")

    monkeypatch.setattr("app.services.managed_drive_pilot.drive_client_from_environment", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_nvidia_vision", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_openai_vision", fail)
    client = TestClient(app)

    response = client.post(
        f"/assets/{asset_id}/edit-metadata",
        headers=auth_header(),
        data={
            "visual_presentation": "not_applicable",
            "visual_presentation_confidence": "0.9",
            "person_visibility": "not_applicable",
            "people_count": "",
            "primary_theme": "transporte",
            "primary_topic": "conducción urbana en primera persona",
            "tags": "conduccion, ciudad",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/assets/{asset_id}?metadata_status=rename_pending"
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        assert asset.status == "ready"
        assert asset.move_status == "moved"
        assert asset.target_name == "old__16x9__c38b9647.mp4"
        assert session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset_id, AssetAIAnalysis.provider == "human")) is not None

    detail = client.get(response.headers["location"], headers=auth_header())
    assert "Metadata actualizada · nombre físico pendiente de sincronizar" in detail.text


def test_web_requests_do_not_call_drive_or_ai(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    app, session_factory = make_test_app(tmp_path)
    asset_id = seed_assets(session_factory)[0]

    def fail(*args, **kwargs):
        raise AssertionError("external call should not run during web review/navigation")

    monkeypatch.setattr("app.web.routes.generate_asset_preview", fail)
    monkeypatch.setattr("app.web.routes.enrich_asset_with_ai", fail)
    monkeypatch.setattr("app.web.routes.scan_source", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.drive_client_from_environment", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_nvidia_vision", fail)
    monkeypatch.setattr("app.services.managed_drive_pilot.call_openai_vision", fail)
    client = TestClient(app)

    assert client.get("/assets", headers=auth_header()).status_code == 200
    assert client.get(f"/assets/{asset_id}", headers=auth_header()).status_code == 200
    assert client.get("/reviews", headers=auth_header()).status_code == 200
    assert client.get(f"/reviews/{asset_id}", headers=auth_header()).status_code == 200
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
            "action": "save_next",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_web_uses_configured_pilot_database_url(tmp_path: Path, monkeypatch) -> None:
    pilot_url = "postgresql+psycopg://asset_hub_pilot:secret@127.0.0.1:55491/kurukin_asset_hub_pilot"
    monkeypatch.setenv("DATABASE_URL", pilot_url)
    monkeypatch.setenv("PILOT_PREVIEW_ROOT", str(tmp_path))
    get_settings.cache_clear()

    assert get_settings().database_url == pilot_url
    assert "kurukin_asset_hub_pilot" in get_settings().database_url
