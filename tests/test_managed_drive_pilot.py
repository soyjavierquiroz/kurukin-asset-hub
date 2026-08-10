from __future__ import annotations

from dataclasses import replace as replace_dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, AssetAIAnalysis, Brand, ManagedDriveFolder
from app.services.managed_drive_pilot import (
    Classification,
    DriveFile,
    DriveAuthError,
    GoogleDriveMutationClient,
    GoogleDriveAPIClient,
    IngestRequest,
    LegacyLayoutMigrationError,
    ManagedAIResult,
    PATH_LAYOUT_VERSION,
    PreflightRequest,
    PreviewResult,
    RcloneDriveClient,
    drive_client_from_environment,
    TechnicalResult,
    ensure_managed_tags,
    compute_plan_hash,
    get_or_create_pilot_source,
    ingest_drive_assets,
    migrate_legacy_layout_assets,
    normalize_primary_topic,
    pilot_orientation,
    primary_topic_slug,
    preflight_drive_access,
    route_parts,
    slugify,
    valid_primary_topic,
    validate_ingest_request,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


def test_path_layout_version_remains_compact_v2() -> None:
    assert PATH_LAYOUT_VERSION == "compact_v2"


class FakeDrive:
    def __init__(
        self,
        fail_move: bool = False,
        drive_id: str | None = None,
        stale_listing: bool = False,
        rollback_leaves_trashed: bool = False,
    ) -> None:
        self.files = {
            "root": DriveFile(
                id="root",
                name="KURUKIN_ASSET_HUB_PILOT",
                mime_type="application/vnd.google-apps.folder",
                parents=[],
                drive_id=drive_id,
                capabilities={"canAddChildren": True, "canEdit": True},
            ),
            "inbox": DriveFile(
                id="inbox",
                name="genericos",
                mime_type="application/vnd.google-apps.folder",
                parents=["root"],
                drive_id=drive_id,
                capabilities={"canAddChildren": True, "canEdit": True},
            ),
            "file-1": DriveFile(
                id="file-1",
                name="Original Video.MP4",
                mime_type="video/mp4",
                parents=["inbox"],
                drive_id=drive_id,
                size=100,
                modified_time=datetime(2026, 8, 3, tzinfo=UTC),
                capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
            )
        }
        self.moves: list[tuple[str, str, str]] = []
        self.restores: list[tuple[str, str, str]] = []
        self.created_folders: list[tuple[str, str]] = []
        self.gets: list[str] = []
        self.fail_move = fail_move
        self.stale_listing = stale_listing
        self.rollback_leaves_trashed = rollback_leaves_trashed

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        if self.stale_listing and folder_id != "inbox":
            return []
        return [item for item in self.files.values() if folder_id in item.parents and not item.trashed][:limit]

    def get_file_metadata(self, file_id: str) -> DriveFile:
        if file_id not in self.files:
            from app.services.managed_drive_pilot import DriveFileNotFoundError

            raise DriveFileNotFoundError("file not found")
        self.gets.append(file_id)
        return self.files[file_id]

    def download_file(self, file_id: str, local_path: Path) -> None:
        local_path.write_bytes(b"test asset")

    def create_folder(self, parent_id: str, name: str) -> str:
        folder_id = f"{parent_id}/{name}"
        self.created_folders.append((parent_id, name))
        self.files[folder_id] = DriveFile(
            id=folder_id,
            name=name,
            mime_type="application/vnd.google-apps.folder",
            parents=[parent_id],
            drive_id=self.files[parent_id].drive_id if parent_id in self.files else None,
        )
        return folder_id

    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile:
        if self.fail_move:
            raise RuntimeError("move failed")
        current = self.files[file_id]
        moved = DriveFile(
            id=file_id,
            name=new_name,
            mime_type=current.mime_type,
            parents=[new_parent_id],
            drive_id=current.drive_id,
            size=current.size,
            modified_time=current.modified_time,
            capabilities=current.capabilities,
            trashed=False,
        )
        self.files[file_id] = moved
        self.moves.append((file_id, new_name, new_parent_id))
        return moved

    def restore_file_location(self, file_id: str, original_name: str, original_parent_id: str) -> DriveFile:
        current = self.files[file_id]
        restored = DriveFile(
            id=file_id,
            name=original_name,
            mime_type=current.mime_type,
            parents=[original_parent_id],
            drive_id=current.drive_id,
            size=current.size,
            modified_time=current.modified_time,
            capabilities=current.capabilities,
            trashed=self.rollback_leaves_trashed,
        )
        self.files[file_id] = restored
        self.restores.append((file_id, original_name, original_parent_id))
        return restored

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        current = self.files[file_id]
        return current.name == expected_name and expected_parent_id in current.parents and not current.trashed

    def folder_exists(self, folder_id: str) -> bool:
        return folder_id in self.files

    def trash_folder(self, folder_id: str) -> None:
        current = self.files[folder_id]
        self.files[folder_id] = replace_dataclass(current, trashed=True)


def technical(_path: Path, _drive_file: DriveFile) -> TechnicalResult:
    return TechnicalResult(
        mime_type="video/mp4",
        media_type="video",
        width=1080,
        height=1920,
        duration_seconds=12.0,
    )


def preview(_asset: Asset, _path: Path, _technical: TechnicalResult) -> PreviewResult:
    return PreviewResult(thumbnail_path="pilot-previews/1/thumbnail.webp", preview_path=None)


def ai(confidence: float = 0.9, generic_compatibility: bool = False):
    def _ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        return ManagedAIResult(
            title_es="persona haciendo yoga",
            description_es="Persona haciendo yoga en una sala luminosa.",
            primary_theme="personas",
            primary_topic="bienestar yoga",
            subject="persona",
            action="haciendo yoga",
            context="sala luminosa",
            tags=["persona", "yoga"],
            suggested_uses=["broll bienestar"],
            can_flip_horizontal=True,
            can_zoom=True,
            generic_compatibility=generic_compatibility,
            camera_motion="static",
            shot_type="wide",
            confidence={"overall": confidence},
            ai_analysis_mode="REAL",
            ai_provider="test",
            ai_model="test-model",
            frames_analyzed=3,
        )

    return _ai


def request_for(scope: str, **kwargs) -> IngestRequest:
    data = {
        "source_folder_id": "inbox",
        "destination_root_id": "root",
        "scope": scope,
        "limit": 10,
        "dry_run": True,
        "apply": False,
    }
    data.update(kwargs)
    return IngestRequest(**data)


def test_scope_generic_validation_rejects_owner() -> None:
    with pytest.raises(ValueError):
        validate_ingest_request(request_for("generic", brand_slug="grandiosa"))


def test_scope_generic_allows_no_brand_or_title() -> None:
    validate_ingest_request(request_for("generic"))


def test_scope_brand_requires_brand() -> None:
    with pytest.raises(ValueError):
        validate_ingest_request(request_for("brand"))


def test_scope_title_requires_title_and_type() -> None:
    with pytest.raises(ValueError):
        validate_ingest_request(request_for("title", title="Rocky"))


def test_orientation_calculation() -> None:
    assert pilot_orientation(1920, 1080) == "horizontal-16x9"
    assert pilot_orientation(1080, 1920) == "vertical-9x16"
    assert pilot_orientation(1080, 1350) == "vertical-4x5"
    assert pilot_orientation(1000, 1000) == "cuadrado-1x1"
    assert pilot_orientation(3000, 1000) == "panoramico"


def test_name_sanitization() -> None:
    assert slugify("Grandiosa Mujer / Acción Ñ") == "grandiosa-mujer-accion-n"


def test_primary_topic_normalizes_hyphens_preserving_accents() -> None:
    assert normalize_primary_topic("análisis-financiero") == "análisis financiero"


def test_primary_topic_normalizes_underscores() -> None:
    assert normalize_primary_topic("mujer_caminando_con_bicicleta") == "mujer caminando con bicicleta"


def test_primary_topic_allows_two_words() -> None:
    assert valid_primary_topic("análisis financiero")


def test_primary_topic_allows_one_word() -> None:
    assert valid_primary_topic("videojuegos")


def test_primary_topic_rejects_more_than_eight_words() -> None:
    assert not valid_primary_topic("uno dos tres cuatro cinco seis siete ocho nueve")


def test_primary_topic_slug_is_generated_from_normalized_text() -> None:
    assert primary_topic_slug("transferencia-de-energía-mágica") == "transferencia-de-energia-magica"


def test_primary_topic_preserves_accents_in_metadata_but_not_slug() -> None:
    topic = normalize_primary_topic("Transferencia_de_Energía_Mágica")
    assert topic == "transferencia de energía mágica"
    assert primary_topic_slug(topic) == "transferencia-de-energia-magica"


def test_generic_route() -> None:
    parts = route_parts(
        request_for("generic"),
        None,
        "video",
        classification("personas", "bienestar yoga"),
        "vertical-9x16",
    )
    assert parts == ["10_genericos", "video", "lote-0001"]


def test_brand_route(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("brand", brand_slug="grandiosa-mujer", collection="evergreen"),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert plan.target_path.startswith("20_marcas/grandiosa-mujer/evergreen/video/lote-0001/")


def test_movie_route(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="movie", title="Rocky"),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert plan.target_path.startswith("30_peliculas_series/peliculas/rocky/brolls-generales/video/lote-0001/")


def test_title_brolls_compact_path(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Mi otra yo"),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert plan.target_path.startswith("30_peliculas_series/series/mi-otra-yo/brolls-generales/video/lote-0001/")


def test_title_season_episode_compact_path(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Mi otra yo", season=1, episode=2),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert plan.target_path.startswith(
        "30_peliculas_series/series/mi-otra-yo/temporada-01/episodio-02/video/lote-0001/"
    )


def test_review_compact_path(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("brand", brand_slug="grandiosa-mujer", collection="evergreen"),
        FakeDrive(),
        technical,
        preview,
        ai(confidence=0.1),
    )[0]

    assert plan.target_path.startswith("90_revision/brand/grandiosa-mujer/video/lote-0001/")


def test_theme_topic_and_orientation_are_not_folders_but_orientation_stays_in_name(session: Session) -> None:
    plan = ingest_drive_assets(session, request_for("generic"), FakeDrive(), technical, preview, ai())[0]
    parent_parts = plan.target_path.split("/")[:-1]

    assert "personas" not in parent_parts
    assert "bienestar-yoga" not in parent_parts
    assert "vertical-9x16" not in parent_parts
    assert "__9x16__" in plan.target_name


def test_series_route(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Nombre Serie", season=1, episode=2),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert "series/nombre-serie/temporada-01/episodio-02/video" in plan.target_path


def test_series_route_without_episode_uses_general_broll(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Nombre Serie"),
        FakeDrive(),
        technical,
        preview,
        ai(),
    )[0]

    assert "series/nombre-serie/brolls-generales/video" in plan.target_path


def test_batch_creation_uses_next_batch(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_FOLDER_BATCH_SIZE", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    drive = FakeDrive()
    first = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]
    assert "lote-0001" in first.target_path

    drive.files["file-2"] = DriveFile(
        id="file-2",
        name="Second.mp4",
        mime_type="video/mp4",
        parents=["inbox"],
        size=101,
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    second = ingest_drive_assets(
        session,
        request_for("generic", file_id="file-2", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]
    assert "lote-0002" in second.target_path


def test_idempotence_by_drive_file_id(session: Session) -> None:
    drive = FakeDrive()
    first = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]
    second = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert first.asset_id == second.asset_id
    assert session.scalar(select(Asset).where(Asset.drive_file_id == "file-1")).id == first.asset_id


def test_ready_moved_asset_in_correct_destination_returns_already_applied(session: Session) -> None:
    drive = FakeDrive()
    first = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]

    second = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert second.result == "already_applied"
    assert second.status == "ready"
    assert second.move_status == "moved"
    assert second.asset_id == first.asset_id


def test_ready_moved_idempotence_does_not_require_original_source_parent(session: Session) -> None:
    drive = FakeDrive()
    ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())
    del drive.files["inbox"]

    second = ingest_drive_assets(
        session,
        request_for("generic", source_folder_id="inbox", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert second.result == "already_applied"


def test_second_apply_has_zero_mutations_no_ai_no_folders_no_count_change_and_no_duplicate(session: Session) -> None:
    from app.models import ManagedDriveFolder

    drive = FakeDrive()
    first = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]
    folder = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.item_count == 1))
    assert folder is not None
    item_count = folder.item_count
    created_folders = list(drive.created_folders)
    moves = list(drive.moves)
    called = False

    def forbidden_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        nonlocal called
        called = True
        raise AssertionError("AI must not be called")

    second = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        forbidden_ai,
    )[0]

    assert first.mutation_stats["mutations_attempted"] == 4
    assert first.mutation_stats["mutations_succeeded"] == 4
    assert second.drive_mutations_performed == 0
    assert second.mutation_stats["mutations_attempted"] == 0
    assert second.mutation_stats["mutations_succeeded"] == 0
    assert second.ai_call is False
    assert called is False
    assert drive.created_folders == created_folders
    assert drive.moves == moves
    assert folder.item_count == item_count
    assert len(session.scalars(select(Asset).where(Asset.drive_file_id == "file-1")).all()) == 1


def test_ready_moved_file_in_trash_blocks_idempotence(session: Session) -> None:
    drive = FakeDrive()
    ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())
    drive.files["file-1"] = replace_dataclass(drive.files["file-1"], trashed=True)

    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.result == "state_mismatch"
    assert plan.status == "review_required"
    assert plan.move_status == "verification_required"
    assert "trashed" in plan.warnings[0]


def test_ready_moved_different_name_blocks_idempotence(session: Session) -> None:
    drive = FakeDrive()
    ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())
    drive.files["file-1"] = replace_dataclass(drive.files["file-1"], name="Different.mp4")

    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.result == "state_mismatch"
    assert plan.move_status == "verification_required"
    assert any("name:" in warning for warning in plan.warnings)


def test_ready_moved_different_parent_blocks_idempotence(session: Session) -> None:
    drive = FakeDrive()
    ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())
    drive.files["other"] = DriveFile(
        id="other",
        name="other",
        mime_type="application/vnd.google-apps.folder",
        parents=["root"],
    )
    drive.files["file-1"] = replace_dataclass(drive.files["file-1"], parents=["other"])

    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.result == "state_mismatch"
    assert plan.move_status == "verification_required"
    assert any("parents:" in warning for warning in plan.warnings)


def test_postgres_moved_drive_inconsistent_goes_to_review_without_mutation(session: Session) -> None:
    drive = FakeDrive()
    ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())
    drive.files["file-1"] = replace_dataclass(drive.files["file-1"], mime_type="video/quicktime")
    created_folders = list(drive.created_folders)
    moves = list(drive.moves)

    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, file_id="file-1"),
        drive,
        technical,
        preview,
        ai(),
    )[0]
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "file-1"))

    assert plan.result == "state_mismatch"
    assert plan.status == "review_required"
    assert plan.move_status == "verification_required"
    assert plan.mutation_stats["mutations_attempted"] == 0
    assert drive.created_folders == created_folders
    assert drive.moves == moves
    assert asset.status == "review_required"
    assert asset.move_status == "verification_required"


def test_reads_and_verifications_do_not_increment_mutation_counters(session: Session) -> None:
    drive = FakeDrive()
    plan = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]

    assert plan.mutation_stats["mutations_attempted"] == 4
    assert plan.mutation_stats["mutations_succeeded"] == 4
    assert plan.drive_mutations_performed == 4


def test_dry_run_does_not_move(session: Session) -> None:
    drive = FakeDrive()
    plan = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]

    assert plan.move_status == "planned"


def test_review_required_persists_concrete_review_reason(session: Session) -> None:
    drive = FakeDrive()

    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Mi otra yo"),
        drive,
        technical,
        preview,
        ai(confidence=0.6),
    )[0]
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "file-1"))

    assert plan.status == "review_required"
    assert plan.requires_review is True
    assert "classification_ambiguous" in plan.warnings
    assert asset.review_reason == "classification_ambiguous"


def test_apply_persisted_review_plan_moves_to_review_and_is_idempotent(session: Session) -> None:
    drive = FakeDrive()
    dry_run = ingest_drive_assets(
        session,
        request_for("title", title_type="series", title="Mi otra yo"),
        drive,
        technical,
        preview,
        ai(confidence=0.6),
    )[0]
    assert dry_run.status == "review_required"

    def no_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        raise AssertionError("AI should not be called when applying a persisted review plan")

    apply_request = request_for(
        "title",
        apply=True,
        dry_run=False,
        file_id="file-1",
        title_type="series",
        title="Mi otra yo",
    )
    applied = ingest_drive_assets(session, apply_request, drive, technical, preview, no_ai)[0]
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "file-1"))
    folder = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == asset.target_parent_id))

    assert applied.result == "applied"
    assert applied.status == "review_required"
    assert applied.move_status == "moved"
    assert applied.requires_review is True
    assert applied.ai_call is False
    assert asset.status == "review_required"
    assert asset.move_status == "moved"
    assert asset.review_reason == "classification_ambiguous"
    assert asset.scope == "title"
    assert asset.title_type == "series"
    assert asset.title_name == "Mi otra yo"
    assert asset.remote_path.startswith("90_revision/title/mi-otra-yo/video/lote-0001/")
    assert drive.files["file-1"].parents == [asset.target_parent_id]
    assert folder.item_count == 1

    move_count = len(drive.moves)
    second = ingest_drive_assets(session, apply_request, drive, technical, preview, no_ai)[0]
    session.refresh(folder)

    assert second.result == "already_applied"
    assert second.status == "review_required"
    assert second.move_status == "moved"
    assert second.mutation_stats["mutations_attempted"] == 0
    assert second.mutation_stats["mutations_succeeded"] == 0
    assert second.ai_call is False
    assert folder.item_count == 1
    assert len(drive.moves) == move_count


def test_apply_moves_and_verifies(session: Session) -> None:
    drive = FakeDrive()
    plan = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]

    assert plan.status == "ready"
    assert plan.move_status == "moved"
    assert drive.moves[0][0] == "file-1"


def test_move_failure_runs_compensation(session: Session) -> None:
    drive = FakeDrive(fail_move=True)
    plan = ingest_drive_assets(session, request_for("generic", apply=True, dry_run=False), drive, technical, preview, ai())[0]

    assert plan.status == "move_planned"
    assert plan.move_status == "planned"
    assert drive.restores


def test_restore_trashed_file_with_drive_api_update() -> None:
    service = FakeDriveService(
        {
            "file-1": {
                "id": "file-1",
                "name": "Original.mp4",
                "mimeType": "video/mp4",
                "parents": ["inbox"],
                "size": "100",
                "trashed": True,
                "capabilities": {"canEdit": True, "canUntrash": True, "canMoveItemWithinDrive": True},
            }
        }
    )
    client = GoogleDriveMutationClient(service)

    restored = client.restore_file_location("file-1", "Original.mp4", "inbox")

    assert restored.trashed is False
    assert service.updates[-1]["body"]["trashed"] is False


def test_drive_api_move_uses_files_update_and_preserves_file_id() -> None:
    service = FakeDriveService(
        {
            "file-1": {
                "id": "file-1",
                "name": "Original.mp4",
                "mimeType": "video/mp4",
                "parents": ["inbox"],
                "size": "100",
                "trashed": False,
            }
        }
    )
    client = GoogleDriveMutationClient(service)

    moved = client.rename_and_move_file("file-1", "Nuevo.mp4", "target", "inbox")

    assert moved.id == "file-1"
    assert moved.name == "Nuevo.mp4"
    assert moved.trashed is False
    assert "target" in moved.parents
    assert "inbox" not in moved.parents
    assert service.updates[-1]["method"] == "update"
    assert service.updates[-1]["addParents"] == "target"
    assert service.updates[-1]["removeParents"] == "inbox"


def test_verification_uses_get_by_id_and_ignores_stale_rclone_listing(session: Session) -> None:
    drive = FakeDrive(stale_listing=True)
    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.status == "ready"
    assert "file-1" in drive.gets


def test_rollback_restores_name_parent_and_trashed_false(session: Session) -> None:
    drive = FakeDrive(fail_move=True)
    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    current = drive.files["file-1"]
    assert plan.status == "move_planned"
    assert plan.move_status == "planned"
    assert current.name == "Original Video.MP4"
    assert current.parents == ["inbox"]
    assert current.trashed is False


def test_rollback_not_successful_if_file_stays_trashed(session: Session) -> None:
    drive = FakeDrive(fail_move=True, rollback_leaves_trashed=True)
    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.status == "review_required"
    assert plan.move_status == "rollback_required"


def test_apply_reuses_persisted_plan_and_does_not_call_ai_or_change_fields(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, dry.asset_id)
    asset.target_name = "approved.mp4"
    asset.remote_path = "10_genericos/video/lote-0001/approved.mp4"
    asset.source_path = asset.remote_path
    asset.primary_topic = "aprobado"

    asset.plan_hash = compute_plan_hash(asset, asset.remote_path, asset.target_name, 1)
    session.commit()
    called = False

    def forbidden_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        nonlocal called
        called = True
        raise AssertionError("AI must not be called")

    applied = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        forbidden_ai,
    )[0]

    assert called is False
    assert applied.target_name == "approved.mp4"
    assert applied.target_path == "10_genericos/video/lote-0001/approved.mp4"
    assert applied.target_batch == 1
    assert applied.path_layout_version == "compact_v2"


def test_persisted_plan_with_legacy_analysis_does_not_change_in_dry_run(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, dry.asset_id)
    assert asset is not None
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="legacy",
            provider="nvidia",
            input_type="preview",
            prompt_version="legacy",
            result_json={"title_es": "Mujer caminando", "tags": ["mujer"]},
            confidence=0.8,
        )
    )
    snapshot = {
        "target_name": asset.target_name,
        "target_path": asset.remote_path,
        "plan_hash": asset.plan_hash,
        "primary_topic": asset.primary_topic,
        "status": asset.status,
        "move_status": asset.move_status,
    }
    session.commit()

    def forbidden_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        raise AssertionError("AI must not be called")

    reused = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, forbidden_ai)[0]
    session.refresh(asset)

    assert reused.target_name == snapshot["target_name"]
    assert asset.target_name == snapshot["target_name"]
    assert asset.remote_path == snapshot["target_path"]
    assert asset.plan_hash == snapshot["plan_hash"]
    assert asset.primary_topic == snapshot["primary_topic"]
    assert asset.status == snapshot["status"]
    assert asset.move_status == snapshot["move_status"]


def test_replan_local_old_layout_does_not_call_ai(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, dry.asset_id)
    assert asset is not None
    asset.remote_path = "10_genericos/video/personas/bienestar-yoga/vertical-9x16/lote-0001/old.mp4"
    asset.source_path = asset.remote_path
    asset.path_layout_version = None
    asset.plan_hash = compute_plan_hash(asset, asset.remote_path, asset.target_name or asset.filename, 1)
    session.commit()

    def forbidden_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        raise AssertionError("AI must not be called")

    replanned = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, forbidden_ai)[0]

    assert replanned.target_path.startswith("10_genericos/video/lote-0001/")
    assert replanned.target_name == dry.target_name
    assert replanned.path_layout_version == "compact_v2"


def test_apply_reuses_compact_v2_plan(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    assert dry.path_layout_version == "compact_v2"

    def forbidden_ai(_asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        raise AssertionError("AI must not be called")

    applied = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        forbidden_ai,
    )[0]

    assert applied.result == "applied"
    assert applied.path_layout_version == "compact_v2"
    assert applied.target_path == dry.target_path


def test_replan_allows_recalculation(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, dry.asset_id)
    old_path = dry.target_path
    asset.target_name = "stale.mp4"
    asset.remote_path = "10_genericos/video/personas/stale/horizontal-16x9/lote-0001/stale.mp4"
    asset.plan_hash = None
    session.commit()

    replanned = ingest_drive_assets(
        session,
        request_for("generic", apply=False, dry_run=True, replan=True),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert replanned.target_path == old_path
    assert replanned.target_name != "stale.mp4"


def test_mutation_counter_keeps_failed_attempts(session: Session) -> None:
    drive = FakeDrive(fail_move=True)
    plan = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert plan.mutation_stats["mutations_attempted"] > 0
    assert plan.mutation_stats["mutations_succeeded"] >= 0


def test_preflight_blocks_apply_with_incomplete_scopes(session: Session) -> None:
    drive = FakeDrive()
    drive.files["file-1"] = DriveFile(
        id="file-1",
        name="Original Video.MP4",
        mime_type="video/mp4",
        parents=["inbox"],
        size=100,
        capabilities={"canEdit": False, "canMoveItemWithinDrive": False},
    )

    with pytest.raises(DriveAuthError):
        ingest_drive_assets(
            session,
            request_for("generic", apply=True, dry_run=False),
            drive,
            technical,
            preview,
            ai(),
        )


def test_failed_apply_does_not_increment_managed_folder_item_count(session: Session) -> None:
    from app.models import ManagedDriveFolder

    drive = FakeDrive(fail_move=True)
    ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )

    folders = session.scalars(select(ManagedDriveFolder)).all()
    assert folders
    assert all(folder.item_count == 0 for folder in folders)


def test_same_asset_is_not_duplicated_after_failed_apply(session: Session) -> None:
    drive = FakeDrive(fail_move=True)
    ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )
    drive.fail_move = False
    ingest_drive_assets(
        session,
        request_for("generic", apply=False, dry_run=True),
        drive,
        technical,
        preview,
        ai(),
    )

    assert len(session.scalars(select(Asset).where(Asset.drive_file_id == "file-1")).all()) == 1


def test_simulate_drive_mutation_builds_update_without_sending(session: Session) -> None:
    from app.models import ManagedDriveFolder

    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, dry.asset_id)
    target_parts = dry.target_path.split("/")[:-1]
    parent_id = "root"
    for part in target_parts:
        parent_id = drive.create_folder(parent_id, part)
    session.add(
        ManagedDriveFolder(
            drive_folder_id=parent_id,
            root_folder_id="root",
            scope=asset.scope,
            brand_id=asset.brand_id,
            title_slug=asset.title_slug,
            title_type=asset.title_type,
            collection=asset.collection,
            media_type=asset.type,
            primary_theme=asset.primary_theme,
            primary_topic=asset.primary_topic,
            orientation=asset.orientation,
            batch_number=1,
        )
    )
    session.commit()
    applied = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False, simulate_drive_mutation=True),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert drive.moves == []
    assert applied.simulated_update["file_id"] == "file-1"
    assert applied.simulated_update["original_parent_id"] == "inbox"
    assert applied.simulated_update["target_parent_id"] == parent_id
    assert applied.simulated_update["target_name"] == asset.target_name


def test_ambiguous_classification_goes_to_review(session: Session) -> None:
    plan = ingest_drive_assets(session, request_for("generic"), FakeDrive(), technical, preview, ai(0.1))[0]

    assert plan.status == "review_required"
    assert plan.target_path.startswith("90_revision/generic/video/lote-0001/")


def test_brand_never_moves_to_generic(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("brand", brand_slug="grandiosa-mujer"),
        FakeDrive(),
        technical,
        preview,
        ai(generic_compatibility=True),
    )[0]

    assert plan.scope == "brand"
    assert plan.target_path.startswith("20_marcas/")


def test_title_never_moves_to_generic(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="movie", title="Rocky"),
        FakeDrive(),
        technical,
        preview,
        ai(generic_compatibility=True),
    )[0]

    assert plan.scope == "title"
    assert plan.target_path.startswith("30_peliculas_series/")


def test_generic_compatibility_does_not_change_scope(session: Session) -> None:
    plan = ingest_drive_assets(
        session,
        request_for("title", title_type="movie", title="Rocky"),
        FakeDrive(),
        technical,
        preview,
        ai(generic_compatibility=True),
    )[0]

    asset = session.get(Asset, plan.asset_id)
    assert asset.scope == "title"
    assert asset.generic_compatibility is True


def test_scope_constraints_exist_on_empty_schema(session: Session) -> None:
    source_request = request_for("generic")
    ingest_drive_assets(session, source_request, FakeDrive(), technical, preview, ai())
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "file-1"))
    asset.scope = "brand"
    asset.brand_id = None

    with pytest.raises(IntegrityError):
        session.commit()


def test_adc_is_default_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    class Credentials:
        valid = True
        expired = False
        token = "adc-token"

        def refresh(self, _request) -> None:
            self.token = "refreshed-adc-token"

    from app.config import get_settings
    import app.services.managed_drive_pilot as managed_drive_pilot

    monkeypatch.delenv("GOOGLE_DRIVE_AUTH_MODE", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_ACCESS_TOKEN", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(managed_drive_pilot, "load_adc_credentials", lambda: Credentials())

    client = GoogleDriveAPIClient.from_environment()

    assert client.auth_mode == "adc"
    assert client._headers()["Authorization"] == "Bearer adc-token"
    get_settings.cache_clear()


def test_access_token_auth_mode_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_DRIVE_AUTH_MODE", "access_token")
    monkeypatch.setenv("GOOGLE_DRIVE_ACCESS_TOKEN", "temporary-token")
    get_settings.cache_clear()

    client = GoogleDriveAPIClient.from_environment()

    assert client.auth_mode == "access_token"
    assert client._headers()["Authorization"] == "Bearer temporary-token"
    get_settings.cache_clear()


def test_missing_access_token_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_DRIVE_AUTH_MODE", "access_token")
    monkeypatch.delenv("GOOGLE_DRIVE_ACCESS_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(DriveAuthError) as exc:
        GoogleDriveAPIClient.from_environment()

    assert "GOOGLE_DRIVE_ACCESS_TOKEN" in str(exc.value)
    assert "Bearer" not in str(exc.value)
    get_settings.cache_clear()


def test_rclone_is_default_drive_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.delenv("GOOGLE_DRIVE_CLIENT", raising=False)
    monkeypatch.setenv("RCLONE_REMOTE", "gdrive_javier")
    get_settings.cache_clear()

    client = drive_client_from_environment()

    assert isinstance(client, RcloneDriveClient)
    assert client.remote == "gdrive_javier"
    get_settings.cache_clear()


def test_rclone_list_folder_uses_root_folder_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess

    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                [
                    {
                        "ID": "file-1",
                        "Name": "Original.mp4",
                        "MimeType": "video/mp4",
                        "Size": 10,
                        "ModTime": "2026-08-03T00:00:00Z",
                        "IsDir": False,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("app.services.managed_drive_pilot.subprocess.run", fake_run)
    client = RcloneDriveClient("gdrive_javier")

    files = client.list_folder("folder-id", 10)

    assert files[0].id == "file-1"
    assert "--drive-root-folder-id" in seen[0]
    assert "folder-id" in seen[0]


def test_rclone_mutation_methods_are_blocked() -> None:
    client = RcloneDriveClient("gdrive_javier")

    with pytest.raises(Exception, match="rclone is read-only"):
        client.rename_and_move_file("file-id", "nuevo.mp4", "parent-id")


def test_preflight_does_not_mutate_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "root")
    for key in (
        "GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_GENERIC_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_BRAND_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_TITLE_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_REVIEW_FOLDER_ID",
        "GOOGLE_DRIVE_ERROR_FOLDER_ID",
    ):
        monkeypatch.setenv(key, f"{key.lower()}-id")
    get_settings.cache_clear()
    drive = FakeDrive(drive_id="shared-drive")

    result = preflight_drive_access(PreflightRequest("inbox", "root", "file-1"), drive)

    assert result.ready_for_dry_run is True
    assert result.drive_mutations_performed == 0
    assert drive.created_folders == []
    assert drive.moves == []
    get_settings.cache_clear()


def test_preflight_blocks_missing_source() -> None:
    with pytest.raises(Exception):
        preflight_drive_access(PreflightRequest("missing", "root"), FakeDrive())


def test_preflight_does_not_treat_absent_move_capability_as_false(session: Session) -> None:
    drive = FakeDrive()
    drive.files["file-1"] = DriveFile(
        id="file-1",
        name="sample.mp4",
        mime_type="video/mp4",
        parents=["inbox"],
        capabilities={"canEdit": True, "canUntrash": True},
    )

    result = preflight_drive_access(PreflightRequest("inbox", "root", "file-1"), drive)

    assert result.can_move_files is True
    assert result.scopes_complete is True


def test_preflight_ignores_empty_folder_placeholder_for_explicit_file_id(session: Session) -> None:
    class PlaceholderDrive(FakeDrive):
        def get_file_metadata(self, file_id: str) -> DriveFile:
            if file_id == "file-1":
                return DriveFile(
                    id="file-1",
                    name="",
                    mime_type="application/vnd.google-apps.folder",
                    capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
                )
            return super().get_file_metadata(file_id)

    result = preflight_drive_access(PreflightRequest("inbox", "root", "file-1"), PlaceholderDrive())

    assert result.test_file_id == "file-1"
    assert result.can_move_files is True
    assert result.scopes_complete is True


def test_managed_drive_pilot_derives_tags_when_ai_returns_empty_tags() -> None:
    ai_result = ManagedAIResult(
        title_es="Silueta de una persona de pie frente a un atardecer",
        description_es="Una persona de pie con la espalda hacia la cámara y cielo con tonos cálidos.",
        primary_theme="personas",
        primary_topic="silueta atardecer",
        tags=[],
        ai_analysis_mode="REAL",
    )

    enriched, warnings = ensure_managed_tags(ai_result, Classification("personas", "silueta atardecer"))

    assert enriched.tags_source == "local_derived"
    assert "tags_generated_locally" in warnings
    assert 5 <= len(enriched.tags) <= 10
    assert "video" not in enriched.tags
    assert "escena" not in enriched.tags


def test_preflight_blocks_missing_destination() -> None:
    with pytest.raises(Exception):
        preflight_drive_access(PreflightRequest("inbox", "missing"), FakeDrive())


def test_source_missing_blocks_dry_run(session: Session) -> None:
    with pytest.raises(Exception):
        ingest_drive_assets(
            session,
            request_for("generic", source_folder_id="missing", file_id="file-1"),
            FakeDrive(),
            technical,
            preview,
            ai(),
        )


def test_destination_missing_blocks_dry_run(session: Session) -> None:
    with pytest.raises(Exception):
        ingest_drive_assets(
            session,
            request_for("generic", destination_root_id="missing", file_id="file-1"),
            FakeDrive(),
            technical,
            preview,
            ai(),
        )


def test_preflight_blocks_different_drives(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "root")
    get_settings.cache_clear()
    drive = FakeDrive(drive_id="drive-a")
    drive.files["root"] = DriveFile(
        id="root",
        name="root",
        mime_type="application/vnd.google-apps.folder",
        drive_id="drive-b",
        capabilities={"canAddChildren": True},
    )

    result = preflight_drive_access(PreflightRequest("inbox", "root", "file-1"), drive)

    assert result.same_drive is False
    assert result.ready_for_dry_run is False
    get_settings.cache_clear()


def test_dry_run_does_not_create_folders_or_mark_ready(session: Session) -> None:
    drive = FakeDrive()
    plan = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    asset = session.get(Asset, plan.asset_id)

    assert drive.created_folders == []
    assert plan.drive_mutations_performed == 0
    assert asset.status == "move_planned"
    assert asset.move_status == "planned"


def test_dry_run_does_not_increment_batch_item_count(session: Session) -> None:
    from app.models import ManagedDriveFolder

    ingest_drive_assets(session, request_for("generic"), FakeDrive(), technical, preview, ai())

    assert session.scalars(select(ManagedDriveFolder)).all() == []


def test_repeating_dry_run_does_not_duplicate_asset(session: Session) -> None:
    drive = FakeDrive()
    first = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    second = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]

    assert first.asset_id == second.asset_id
    assert len(session.scalars(select(Asset).where(Asset.drive_file_id == "file-1")).all()) == 1


def test_apply_after_dry_run_reuses_same_asset(session: Session) -> None:
    drive = FakeDrive()
    dry = ingest_drive_assets(session, request_for("generic"), drive, technical, preview, ai())[0]
    applied = ingest_drive_assets(
        session,
        request_for("generic", apply=True, dry_run=False),
        drive,
        technical,
        preview,
        ai(),
    )[0]

    assert applied.asset_id == dry.asset_id
    assert applied.status == "ready"


def add_legacy_asset(
    session: Session,
    drive: FakeDrive,
    drive_file_id: str = "legacy-1",
    scope: str = "generic",
    status: str = "ready",
    old_parent_id: str = "old-leaf",
    old_path: str = "10_genericos/video/personas/bienestar-yoga/vertical-9x16/lote-0001/name.mp4",
) -> Asset:
    source = get_or_create_pilot_source(session)
    brand = None
    if scope == "brand":
        brand = Brand(slug="grandiosa-mujer", name="Grandiosa Mujer")
        session.add(brand)
        session.flush()
    drive.files[old_parent_id] = DriveFile(
        id=old_parent_id,
        name=old_parent_id,
        mime_type="application/vnd.google-apps.folder",
        parents=["old-topic"],
    )
    drive.files["old-topic"] = DriveFile(
        id="old-topic",
        name="bienestar-yoga",
        mime_type="application/vnd.google-apps.folder",
        parents=["old-theme"],
    )
    drive.files["old-theme"] = DriveFile(
        id="old-theme",
        name="personas",
        mime_type="application/vnd.google-apps.folder",
        parents=["root"],
    )
    drive.files[drive_file_id] = DriveFile(
        id=drive_file_id,
        name="name.mp4",
        mime_type="video/mp4",
        parents=[old_parent_id],
        size=123,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    asset = Asset(
        asset_uid=f"drive-{drive_file_id}",
        source=source,
        provider="google_drive",
        remote_path=old_path,
        source_path=old_path,
        drive_file_id=drive_file_id,
        remote_file_id=drive_file_id,
        filename="name.mp4",
        original_name="original.mp4",
        original_parent_id="inbox",
        target_parent_id=old_parent_id,
        target_name="name.mp4",
        scope=scope,
        brand=brand,
        title_type="series" if scope == "title" else None,
        title_name="Mi otra yo" if scope == "title" else None,
        title_slug="mi-otra-yo" if scope == "title" else None,
        collection="evergreen" if scope == "brand" else None,
        type="video",
        mime_type="video/mp4",
        primary_theme="personas",
        primary_topic="bienestar yoga",
        orientation="vertical-9x16",
        thumbnail_path="pilot-previews/1/thumbnail.webp",
        status=status,
        move_status="moved",
        review_reason="classification_ambiguous" if status == "review_required" else None,
        path_layout_version=None,
    )
    session.add(asset)
    session.flush()
    session.add(
        ManagedDriveFolder(
            drive_folder_id=old_parent_id,
            root_folder_id="root",
            scope=scope,
            brand_id=brand.id if brand else None,
            title_slug=asset.title_slug,
            title_type=asset.title_type,
            collection=asset.collection,
            media_type="video",
            primary_theme="personas",
            primary_topic="bienestar yoga",
            orientation="vertical-9x16",
            batch_number=1,
            item_count=1,
        )
    )
    session.commit()
    return asset


def test_legacy_audit_detects_only_legacy_assets(session: Session) -> None:
    drive = FakeDrive()
    legacy = add_legacy_asset(session, drive)
    compact = add_legacy_asset(
        session,
        drive,
        drive_file_id="compact-1",
        old_parent_id="compact-folder",
        old_path="10_genericos/video/lote-0001/compact.mp4",
    )
    compact.path_layout_version = "compact_v2"
    compact.remote_path = "10_genericos/video/lote-0001/name.mp4"
    session.commit()

    result = migrate_legacy_layout_assets(session, drive, "root", {legacy.drive_file_id}, simulate=True)

    assert result.assets_detected == [legacy.drive_file_id]
    assert result.drive_mutations == 0


def test_legacy_migration_conserves_file_id_name_and_moves_item_count(session: Session) -> None:
    drive = FakeDrive()
    asset = add_legacy_asset(session, drive)

    result = migrate_legacy_layout_assets(session, drive, "root", {asset.drive_file_id}, simulate=False)
    session.refresh(asset)
    old_folder = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == "old-leaf"))
    compact_folder = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.path_layout_version == "compact_v2"))

    assert result.assets_migrated == 1
    assert drive.files[asset.drive_file_id].id == asset.drive_file_id
    assert drive.files[asset.drive_file_id].name == "name.mp4"
    assert asset.remote_path == "10_genericos/video/lote-0001/name.mp4"
    assert asset.path_layout_version == "compact_v2"
    assert old_folder.item_count == 0
    assert compact_folder.item_count == 1


def test_legacy_migration_preserves_review_required(session: Session) -> None:
    drive = FakeDrive()
    asset = add_legacy_asset(
        session,
        drive,
        drive_file_id="review-1",
        scope="title",
        status="review_required",
        old_path="90_revision/clasificacion-ambigua/name.mp4",
    )

    migrate_legacy_layout_assets(session, drive, "root", {asset.drive_file_id}, simulate=False)
    session.refresh(asset)

    assert asset.status == "review_required"
    assert asset.move_status == "moved"
    assert asset.review_reason == "classification_ambiguous"
    assert asset.scope == "title"
    assert asset.remote_path == "90_revision/title/mi-otra-yo/video/lote-0001/name.mp4"


def test_legacy_cleanup_does_not_delete_non_empty_or_protected_folder(session: Session) -> None:
    drive = FakeDrive()
    asset = add_legacy_asset(session, drive)
    drive.files["stray"] = DriveFile(id="stray", name="stray.mp4", mime_type="video/mp4", parents=["old-topic"])

    result = migrate_legacy_layout_assets(
        session,
        drive,
        "root",
        {asset.drive_file_id},
        simulate=False,
        cleanup_empty_folders=True,
    )

    assert "old-leaf" in result.cleaned_folder_ids
    assert "old-topic" in result.not_deleted_folder_ids
    assert drive.files["old-theme"].trashed is False
    assert drive.files["root"].trashed is False


def test_legacy_cleanup_bottom_up(session: Session) -> None:
    drive = FakeDrive()
    asset = add_legacy_asset(session, drive)

    result = migrate_legacy_layout_assets(
        session,
        drive,
        "root",
        {asset.drive_file_id},
        simulate=False,
        cleanup_empty_folders=True,
    )

    assert result.cleaned_folder_ids[:3] == ["old-leaf", "old-topic", "old-theme"]


def test_legacy_migration_second_run_noop_and_zero_ai(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.ai_asset_enrichment as enrichment
    import app.services.managed_drive_pilot as pilot

    monkeypatch.setattr(enrichment, "call_openai_vision", lambda *_args, **_kwargs: pytest.fail("OpenAI used"))
    monkeypatch.setattr(pilot, "call_nvidia_vision", lambda *_args, **_kwargs: pytest.fail("NVIDIA used"))
    drive = FakeDrive()
    asset = add_legacy_asset(session, drive)
    migrate_legacy_layout_assets(session, drive, "root", {asset.drive_file_id}, simulate=False, cleanup_empty_folders=True)

    second = migrate_legacy_layout_assets(session, drive, "root", set(), simulate=False, cleanup_empty_folders=True)

    assert second.assets_migrated == 0
    assert second.folders_created == 0
    assert second.folders_deleted == 0
    assert second.drive_mutations == 0
    assert second.duplicates == 0


def test_legacy_migration_fails_on_unexpected_asset_before_mutating(session: Session) -> None:
    drive = FakeDrive()
    add_legacy_asset(session, drive)

    with pytest.raises(LegacyLayoutMigrationError):
        migrate_legacy_layout_assets(session, drive, "root", {"other"}, simulate=False)

    assert drive.moves == []


class FakeDriveService:
    def __init__(self, files: dict[str, dict]) -> None:
        self.files_data = files
        self.updates: list[dict] = []
        self._pending: dict | None = None

    def files(self):
        return self

    def get(self, **kwargs):
        self._pending = {"method": "get", **kwargs}
        return self

    def update(self, **kwargs):
        self._pending = {"method": "update", **kwargs}
        self.updates.append(self._pending)
        return self

    def list(self, **kwargs):
        self._pending = {"method": "list", **kwargs}
        return self

    def create(self, **kwargs):
        self._pending = {"method": "create", **kwargs}
        return self

    def execute(self):
        assert self._pending is not None
        method = self._pending["method"]
        if method == "get":
            return self.files_data[self._pending["fileId"]]
        if method == "update":
            file_id = self._pending["fileId"]
            current = dict(self.files_data[file_id])
            body = self._pending.get("body") or {}
            current.update(body)
            add_parent = self._pending.get("addParents")
            remove_parent = self._pending.get("removeParents")
            parents = [parent for parent in current.get("parents", []) if parent != remove_parent]
            if add_parent and add_parent not in parents:
                parents.append(add_parent)
            current["parents"] = parents
            self.files_data[file_id] = current
            return current
        if method == "list":
            return {"files": []}
        if method == "create":
            body = self._pending["body"]
            file_id = f"{body['parents'][0]}/{body['name']}"
            self.files_data[file_id] = {
                "id": file_id,
                "name": body["name"],
                "mimeType": body["mimeType"],
                "parents": body["parents"],
                "trashed": False,
            }
            return {"id": file_id}
        raise AssertionError(method)


def classification(theme: str, topic: str):
    from app.services.managed_drive_pilot import Classification

    return Classification(theme, topic)
