from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.config import get_settings
from app.models import Asset, ManagedDriveFolder
from app.services.ai_providers.nvidia_provider import (
    NvidiaProviderError,
    NvidiaReducedAssetResult,
    parse_reduced_result,
    reduced_to_asset_result,
)
from app.services.managed_drive_pilot import (
    BatchRequest,
    BatchScope,
    DriveFile,
    ManagedAIResult,
    PreviewResult,
    TechnicalResult,
    run_drive_batch,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


class BatchFakeDrive:
    def __init__(self, counts: dict[str, int] | None = None, fail_move: bool = False) -> None:
        counts = counts or {"generic": 1, "brand": 1, "title": 1}
        self.files: dict[str, DriveFile] = {
            "root": folder("root", []),
            "generic-inbox": folder("generic-inbox", ["root"]),
            "brand-inbox": folder("brand-inbox", ["root"]),
            "title-inbox": folder("title-inbox", ["root"]),
        }
        for scope, folder_id in (
            ("generic", "generic-inbox"),
            ("brand", "brand-inbox"),
            ("title", "title-inbox"),
        ):
            for index in range(1, counts.get(scope, 0) + 1):
                file_id = f"{scope}-{index}"
                self.files[file_id] = DriveFile(
                    id=file_id,
                    name=f"{scope}-{index}.mp4",
                    mime_type="video/mp4",
                    parents=[folder_id],
                    size=100 + index,
                    modified_time=datetime(2026, 8, 3, tzinfo=UTC),
                    capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
                )
        self.files["missing-parent"] = folder("missing-parent", ["root"])
        self.moves: list[tuple[str, str, str]] = []
        self.created_folders: list[tuple[str, str]] = []
        self.fail_move = fail_move

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        return [item for item in self.files.values() if folder_id in item.parents and not item.is_folder][:limit]

    def get_file_metadata(self, file_id: str) -> DriveFile:
        return self.files[file_id]

    def download_file(self, file_id: str, local_path: Path) -> None:
        local_path.write_bytes(f"asset {file_id}".encode())

    def create_folder(self, parent_id: str, name: str) -> str:
        folder_id = f"{parent_id}/{name}"
        self.created_folders.append((parent_id, name))
        self.files[folder_id] = folder(folder_id, [parent_id])
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
            size=current.size,
            modified_time=current.modified_time,
            capabilities=current.capabilities,
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
            size=current.size,
            modified_time=current.modified_time,
            capabilities=current.capabilities,
        )
        self.files[file_id] = restored
        return restored

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        current = self.files[file_id]
        return current.name == expected_name and current.parents == [expected_parent_id]

    def folder_exists(self, folder_id: str) -> bool:
        return folder_id in self.files


def folder(file_id: str, parents: list[str]) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=file_id,
        mime_type="application/vnd.google-apps.folder",
        parents=parents,
        capabilities={"canAddChildren": True, "canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )


def scopes() -> list[BatchScope]:
    return [
        BatchScope("generic", "generic-inbox", "root"),
        BatchScope("brand", "brand-inbox", "root", brand_slug="grandiosa-mujer", collection="evergreen"),
        BatchScope("title", "title-inbox", "root", title_type="series", title="Mi otra yo"),
    ]


def technical(_path: Path, _drive_file: DriveFile) -> TechnicalResult:
    return TechnicalResult("video/mp4", "video", 1920, 1080, 8.0)


def preview(_asset: Asset, _path: Path, _technical: TechnicalResult) -> PreviewResult:
    return PreviewResult("pilot-previews/1/thumbnail.webp", None)


def ai(order: list[str] | None = None, confidence: float = 0.9, requires_review: bool = False):
    def _ai(asset: Asset, _path: Path, _preview: PreviewResult) -> ManagedAIResult:
        if order is not None:
            order.append(asset.drive_file_id or "")
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
            confidence={"overall": confidence},
            requires_review=requires_review,
            warnings=["classification_ambiguous"] if requires_review else [],
            ai_analysis_mode="REAL",
            ai_provider="nvidia",
            ai_model="nvidia/nemotron-nano-12b-v2-vl",
            frames_analyzed=3,
        )

    return _ai


def run(
    session: Session,
    drive: BatchFakeDrive,
    request: BatchRequest | None = None,
    ai_enricher=None,
):
    return run_drive_batch(
        session,
        request or BatchRequest(nvidia_pause_seconds=0),
        drive,
        technical,
        preview,
        ai_enricher or ai(),
        drive,
        scopes(),
    )


def test_batch_without_new_files_returns_noop(session: Session) -> None:
    result = run(session, BatchFakeDrive({"generic": 0, "brand": 0, "title": 0}))

    assert result.counters.files_new == 0
    assert result.assets == []


def test_batch_respects_max_total(session: Session) -> None:
    result = run(session, BatchFakeDrive({"generic": 4, "brand": 4, "title": 4}), BatchRequest(max_total=2))

    assert result.counters.files_new == 2
    assert len(result.assets) == 2


def test_batch_respects_max_per_scope(session: Session) -> None:
    result = run(session, BatchFakeDrive({"generic": 3, "brand": 3, "title": 0}), BatchRequest(max_total=10, max_per_scope=2))

    assert [asset.scope for asset in result.assets].count("generic") == 2
    assert [asset.scope for asset in result.assets].count("brand") == 2


def test_batch_processes_sequentially(session: Session) -> None:
    order: list[str] = []
    run(session, BatchFakeDrive({"generic": 2, "brand": 1, "title": 0}), ai_enricher=ai(order))

    assert order == ["generic-1", "generic-2", "brand-1"]


def test_batch_skips_ready_moved_and_review_moved(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=2), ai())
    run(session, drive, BatchRequest(apply=True), ai())
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2"))
    asset.status = "review_required"
    asset.move_status = "moved"
    asset.review_reason = "classification_ambiguous"
    drive.files["generic-1"] = drive.files["generic-1"].__class__(**{**drive.files["generic-1"].__dict__, "parents": ["generic-inbox"]})
    drive.files["generic-2"] = drive.files["generic-2"].__class__(**{**drive.files["generic-2"].__dict__, "parents": ["generic-inbox"]})
    session.commit()

    second = run(session, drive)

    assert second.counters.files_skipped == 2
    assert second.counters.files_analyzed == 0


def test_batch_reuses_move_planned_plan_and_apply_does_not_call_ai(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    dry = run(session, drive)
    assert dry.assets[0].final_status == "move_planned"

    def forbidden(*_args):
        raise AssertionError("AI must not be called for persisted plan apply")

    applied = run(session, drive, BatchRequest(apply=True), forbidden)

    assert applied.counters.files_analyzed == 0
    assert applied.counters.files_applied == 1
    assert applied.counters.nvidia_requests == 0
    assert applied.counters.openai_requests == 0


def test_apply_does_not_analyze_new_files_and_reports_pending_analysis(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    drive.files["generic-2"] = DriveFile(
        id="generic-2",
        name="generic-2.mp4",
        mime_type="video/mp4",
        parents=["generic-inbox"],
        size=102,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )

    def forbidden(*_args):
        raise AssertionError("AI must not be called for batch apply")

    applied = run(session, drive, BatchRequest(apply=True), forbidden)

    assert applied.counters.files_applied == 1
    assert applied.counters.files_analyzed == 0
    assert applied.counters.nvidia_requests == 0
    assert applied.counters.openai_requests == 0
    assert applied.counters.pending_analysis == 1
    pending = [asset for asset in applied.assets if asset.pending_analysis]
    assert [asset.drive_file_id for asset in pending] == ["generic-2"]
    assert session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2")) is None
    assert drive.files["generic-2"].parents == ["generic-inbox"]
    assert drive.files["generic-2"].name == "generic-2.mp4"


def test_approved_routes_by_scope(session: Session) -> None:
    result = run(session, BatchFakeDrive({"generic": 1, "brand": 1, "title": 1}))
    paths = {asset.scope: asset.target_path for asset in result.assets}

    assert paths["generic"].startswith("10_genericos/")
    assert paths["brand"].startswith("20_marcas/grandiosa-mujer/")
    assert paths["title"].startswith("30_peliculas_series/series/mi-otra-yo/")


def test_ambiguous_asset_goes_to_review_with_reason(session: Session) -> None:
    result = run(session, BatchFakeDrive({"generic": 1, "brand": 0, "title": 0}), ai_enricher=ai(requires_review=True))

    assert result.assets[0].target_path.startswith("90_revision/")
    assert result.assets[0].review_reason is not None


def test_nvidia_error_does_not_move_and_retries_once(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.managed_drive_pilot as pilot

    calls = 0

    def fail_nvidia(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise NvidiaProviderError("429 rate limit")

    monkeypatch.setattr(pilot, "call_nvidia_vision", fail_nvidia)
    result = run_drive_batch(
        session,
        BatchRequest(nvidia_pause_seconds=0),
        BatchFakeDrive({"generic": 1, "brand": 0, "title": 0}),
        technical_analyzer=technical,
        preview_generator=preview,
        mutation_client=BatchFakeDrive({"generic": 1, "brand": 0, "title": 0}),
        scopes=scopes(),
    )

    assert calls == 2
    assert result.counters.nvidia_retries == 1
    assert result.counters.files_failed == 1
    assert result.counters.drive_mutations_succeeded == 0


def test_no_openai_fallback(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.ai_asset_enrichment as enrichment
    import app.services.managed_drive_pilot as pilot

    monkeypatch.setattr(enrichment, "call_openai_vision", lambda *_args, **_kwargs: pytest.fail("OpenAI fallback used"))
    monkeypatch.setattr(
        pilot,
        "call_nvidia_vision",
        lambda *_args, **_kwargs: reduced_to_asset_result(
            NvidiaReducedAssetResult(
                title_es="persona haciendo yoga",
                description_es="Persona haciendo yoga.",
                primary_theme="personas",
                primary_topic="bienestar yoga",
                tags=["persona", "yoga"],
                suggested_uses=["broll"],
                has_visible_text=False,
                has_logo=False,
                can_flip_horizontal=True,
                can_zoom=True,
                max_safe_zoom=1.0,
                generic_compatibility=True,
                requires_review=False,
            )
        ),
    )

    result = run_drive_batch(
        session,
        BatchRequest(nvidia_pause_seconds=0),
        BatchFakeDrive({"generic": 1, "brand": 0, "title": 0}),
        technical_analyzer=technical,
        preview_generator=preview,
        mutation_client=BatchFakeDrive({"generic": 1, "brand": 0, "title": 0}),
        scopes=scopes(),
    )

    assert result.counters.openai_requests == 0
    assert result.counters.nvidia_requests == 1


def test_nvidia_reduced_schema_accepts_legacy_payload_without_people_fields() -> None:
    result = parse_reduced_result(
        """
        {
          "title_es": "persona haciendo yoga",
          "description_es": "Persona haciendo yoga.",
          "primary_theme": "personas",
          "primary_topic": "bienestar yoga",
          "tags": ["persona", "yoga"],
          "suggested_uses": ["broll"],
          "has_visible_text": false,
          "has_logo": false,
          "can_flip_horizontal": true,
          "flip_risk_reasons": [],
          "can_zoom": true,
          "max_safe_zoom": 1.0,
          "generic_compatibility": true,
          "requires_review": false,
          "warnings": []
        }
        """
    )

    assert result.contains_people is False
    assert result.people_count is None
    assert result.visual_presentation == "not_applicable"
    assert result.person_visibility == "not_applicable"
    assert result.search_terms == []


def test_nvidia_reduced_schema_keeps_people_search_terms_without_network() -> None:
    reduced = parse_reduced_result(
        """
        {
          "title_es": "grupo de personas caminando",
          "description_es": "Grupo mixto de personas caminando.",
          "primary_theme": "personas",
          "primary_topic": "grupo caminando",
          "tags": ["grupo", "personas"],
          "suggested_uses": ["broll"],
          "contains_people": true,
          "people_count": 3,
          "visual_presentation": "mixed",
          "visual_presentation_confidence": 0.91,
          "person_visibility": "clear",
          "search_terms": ["personas", "hombres", "mujeres", "grupo"],
          "has_visible_text": false,
          "has_logo": false,
          "can_flip_horizontal": true,
          "flip_risk_reasons": [],
          "can_zoom": true,
          "max_safe_zoom": 1.0,
          "generic_compatibility": true,
          "requires_review": false,
          "warnings": []
        }
        """
    )
    result = reduced_to_asset_result(reduced)

    assert reduced.search_terms == ["personas", "hombres", "mujeres", "grupo"]
    assert result.visual_presentation == "mixed"
    assert {"personas", "hombres", "mujeres", "grupo"}.issubset(result.search_terms)
    assert "hombres" in result.search_text


def test_second_execution_is_idempotent_and_item_count_not_duplicated(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    first = run(session, drive, BatchRequest(apply=True), ai())
    folder_row = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.item_count == 1))
    assert folder_row is not None

    second = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())
    session.refresh(folder_row)

    assert first.counters.files_applied == 1
    assert second.counters.already_applied == 1
    assert second.counters.drive_mutations_attempted == 0
    assert folder_row.item_count == 1


def test_allowlist_processes_only_selected_ids(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=2), ai())

    applied = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())

    first = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    second = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2"))
    assert applied.counters.files_applied == 1
    assert first.move_status == "moved"
    assert second.move_status == "planned"
    assert drive.files["generic-2"].parents == ["generic-inbox"]


def test_allowlist_missing_id_fails_before_mutation(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)

    with pytest.raises(ValueError, match="not registered"):
        run(session, drive, BatchRequest(apply=True, only_file_ids=("missing",)), ai())

    assert drive.moves == []
    assert drive.created_folders == []


def test_allowlist_null_plan_hash_fails_before_mutation(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    asset.plan_hash = None
    session.commit()

    with pytest.raises(ValueError, match="null plan_hash"):
        run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())

    assert drive.moves == []
    assert drive.created_folders == []


def test_second_apply_with_new_file_still_pending_is_noop(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    drive.files["generic-2"] = DriveFile(
        id="generic-2",
        name="generic-2.mp4",
        mime_type="video/mp4",
        parents=["generic-inbox"],
        size=102,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    first = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())
    folder_row = session.scalar(select(ManagedDriveFolder).where(ManagedDriveFolder.item_count == 1))
    assert folder_row is not None

    second = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())
    session.refresh(folder_row)

    assert first.counters.files_applied == 1
    assert second.counters.files_applied == 0
    assert second.counters.already_applied == 1
    assert second.counters.pending_analysis == 1
    assert second.counters.drive_mutations_attempted == 0
    assert second.counters.drive_mutations_succeeded == 0
    assert folder_row.item_count == 1


def test_dry_run_after_apply_discovers_pending_file(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    drive.files["generic-2"] = DriveFile(
        id="generic-2",
        name="generic-2.mp4",
        mime_type="video/mp4",
        parents=["generic-inbox"],
        size=102,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())

    dry = run(session, drive)

    assert dry.counters.files_new == 1
    assert dry.counters.files_analyzed == 1
    assert dry.assets[0].drive_file_id == "generic-2"


def test_batch_apply_releases_lock_on_allowlist_validation_error(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.managed_drive_pilot as pilot

    released = 0

    def release(_session: Session) -> None:
        nonlocal released
        released += 1

    monkeypatch.setattr(pilot, "release_batch_lock", release)

    with pytest.raises(ValueError):
        run(session, BatchFakeDrive({"generic": 0, "brand": 0, "title": 0}), BatchRequest(apply=True, only_file_ids=("missing",)))

    assert released == 1


def test_second_dry_run_reuses_plan_without_ai(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    first = run(session, drive)
    assert first.counters.files_analyzed == 1

    def forbidden(*_args):
        raise AssertionError("AI must not be called for persisted dry-run plan")

    second = run(session, drive, ai_enricher=forbidden)

    assert second.counters.files_new == 1
    assert second.counters.files_analyzed == 0
    assert second.assets[0].analysis_result == "planned"


def test_compact_batch_allows_250_per_leaf_context(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 250, "brand": 0, "title": 0})
    result = run(
        session,
        drive,
        BatchRequest(max_total=250, max_per_scope=250, nvidia_pause_seconds=0),
        ai(),
    )

    assert result.counters.files_new == 250
    assert {asset.target_path.split("/")[-2] for asset in result.assets} == {"lote-0001"}


def test_compact_batch_starts_new_lote_after_250(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANAGED_FOLDER_BATCH_SIZE", "250")
    get_settings.cache_clear()
    drive = BatchFakeDrive({"generic": 251, "brand": 0, "title": 0})
    result = run(
        session,
        drive,
        BatchRequest(max_total=251, max_per_scope=251, nvidia_pause_seconds=0),
        ai(),
    )
    get_settings.cache_clear()

    assert result.assets[249].target_path.split("/")[-2] == "lote-0001"
    assert result.assets[250].target_path.split("/")[-2] == "lote-0002"


def test_advisory_lock_blocks_second_batch(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.managed_drive_pilot as pilot

    monkeypatch.setattr(pilot, "acquire_batch_lock", lambda _session: False)
    result = run(session, BatchFakeDrive())

    assert result.lock_acquired is False
    assert result.counters.files_discovered == 0


def test_dry_run_produces_zero_drive_mutations(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 1, "title": 1})
    result = run(session, drive)

    assert result.counters.drive_mutations_attempted == 0
    assert drive.moves == []
