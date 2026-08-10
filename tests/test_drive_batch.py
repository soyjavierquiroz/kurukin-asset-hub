from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.config import get_settings
from app.models import Asset, AssetAIAnalysis, ManagedDriveFolder, Source
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
    ReviewApproval,
    TechnicalResult,
    approve_review_asset,
    compute_plan_hash,
    rename_reviewed_moved_asset,
    replan_reviewed_assets,
    run_drive_batch,
    short_id,
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
            "brand-grandiosa-mujer-inbox": folder(
                "brand-grandiosa-mujer-inbox",
                ["brand-inbox"],
                name="grandiosa-mujer",
            ),
            "title-inbox": folder("title-inbox", ["root"]),
            "title-mi-otra-yo-inbox": folder(
                "title-mi-otra-yo-inbox",
                ["title-inbox"],
                name="mi-otra-yo",
            ),
        }
        for scope, folder_id in (
            ("generic", "generic-inbox"),
            ("brand", "brand-inbox"),
            ("title", "title-inbox"),
        ):
            parents = [folder_id]
            if scope == "brand":
                parents.append("brand-grandiosa-mujer-inbox")
            if scope == "title":
                parents.append("title-mi-otra-yo-inbox")
            for index in range(1, counts.get(scope, 0) + 1):
                file_id = f"{scope}-{index}"
                self.files[file_id] = DriveFile(
                    id=file_id,
                    name=f"{scope}-{index}.mp4",
                    mime_type="video/mp4",
                    parents=parents,
                    size=100 + index,
                    modified_time=datetime(2026, 8, 3, tzinfo=UTC),
                    capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
                )
        self.files["missing-parent"] = folder("missing-parent", ["root"])
        self.moves: list[tuple[str, str, str]] = []
        self.created_folders: list[tuple[str, str]] = []
        self.list_calls: list[tuple[str, int]] = []
        self.find_child_folder_calls: list[tuple[str, str]] = []
        self.metadata_calls: list[str] = []
        self.fail_move = fail_move

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        self.list_calls.append((folder_id, limit))
        return [
            item.__class__(**{**item.__dict__, "parents": [folder_id]})
            for item in self.files.values()
            if folder_id in item.parents and not item.is_folder
        ][:limit]

    def find_child_folder_id(self, parent_id: str, name: str) -> str | None:
        self.find_child_folder_calls.append((parent_id, name))
        for item in self.files.values():
            if item.is_folder and parent_id in item.parents and item.name == name:
                return item.id
        return None

    def get_file_metadata(self, file_id: str) -> DriveFile:
        self.metadata_calls.append(file_id)
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


class ForbiddenDrive(BatchFakeDrive):
    def __init__(self) -> None:
        super().__init__({"generic": 0, "brand": 0, "title": 0})

    def get_file_metadata(self, file_id: str) -> DriveFile:
        raise AssertionError(f"Drive must not be called for {file_id}")

    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile:
        raise AssertionError(f"Drive must not be mutated for {file_id}")


def folder(file_id: str, parents: list[str], name: str | None = None) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name or file_id,
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


def leaf_scopes() -> list[BatchScope]:
    return [
        BatchScope("generic", "generic-inbox", "root"),
        BatchScope(
            "brand",
            "brand-grandiosa-mujer-inbox",
            "root",
            brand_slug="grandiosa-mujer",
            collection="evergreen",
        ),
        BatchScope("title", "title-mi-otra-yo-inbox", "root", title_type="series", title="Mi otra yo"),
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


def english_driving_ai(*_args) -> ManagedAIResult:
    return ManagedAIResult(
        title_es="someone is driving a car on a city street with a blue",
        description_es="Video urbano desde un auto.",
        primary_theme="transporte",
        primary_topic="city street driving",
        subject="someone",
        action="is driving a car",
        context="on a city street with a blue",
        tags=["car", "city"],
        confidence={"overall": 0.9},
        requires_review=True,
        warnings=["ai_requires_review"],
        ai_analysis_mode="REAL",
        ai_provider="nvidia",
        ai_model="nvidia/nemotron-nano-12b-v2-vl",
        frames_analyzed=3,
    )


def invalid_placeholder_ai(*_args) -> ManagedAIResult:
    return ManagedAIResult(
        title_es="revision manual",
        description_es="Respuesta NVIDIA invalida para source.mp4.",
        primary_theme="otros",
        primary_topic="revision manual",
        tags=[],
        confidence={"overall": 0.0},
        requires_review=True,
        warnings=["ai_response_invalid"],
        ai_analysis_mode="REAL",
        ai_provider="nvidia",
        ai_model="nvidia/nemotron-nano-12b-v2-vl",
        frames_analyzed=0,
    )


def run(
    session: Session,
    drive: BatchFakeDrive,
    request: BatchRequest | None = None,
    ai_enricher=None,
    batch_scopes: list[BatchScope] | None = None,
):
    return run_drive_batch(
        session,
        request or BatchRequest(nvidia_pause_seconds=0),
        drive,
        technical,
        preview,
        ai_enricher or ai(),
        drive,
        batch_scopes or scopes(),
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


def test_scope_generic_discovers_only_generic(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    result = run(session, drive, BatchRequest(scope="generic", max_total=10, max_per_scope=10))

    assert {asset.scope for asset in result.assets} == {"generic"}
    assert drive.list_calls == [("generic-inbox", 10)]


def test_scope_title_uses_configured_leaf_when_it_matches_requested_slug(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    result = run(
        session,
        drive,
        BatchRequest(scope="title", title_slug="mi-otra-yo", max_total=10, max_per_scope=10),
        batch_scopes=leaf_scopes(),
    )

    assert {asset.scope for asset in result.assets} == {"title"}
    assert [asset.drive_file_id for asset in result.assets] == ["title-1", "title-2"]
    assert drive.metadata_calls[0] == "title-mi-otra-yo-inbox"
    assert drive.find_child_folder_calls == []
    assert drive.list_calls == [("title-mi-otra-yo-inbox", 10)]


def test_scope_title_discovers_selected_title_child_when_configured_folder_is_parent(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    result = run(
        session,
        drive,
        BatchRequest(scope="title", title_slug="mi-otra-yo", max_total=10, max_per_scope=10),
    )

    assert {asset.scope for asset in result.assets} == {"title"}
    assert [asset.drive_file_id for asset in result.assets] == ["title-1", "title-2"]
    assert drive.metadata_calls[0] == "title-inbox"
    assert drive.find_child_folder_calls == [("title-inbox", "mi-otra-yo")]
    assert drive.list_calls == [("title-mi-otra-yo-inbox", 10)]


def test_scope_brand_uses_configured_leaf_when_it_matches_requested_slug(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    result = run(
        session,
        drive,
        BatchRequest(scope="brand", brand_slug="grandiosa-mujer", max_total=10, max_per_scope=10),
        batch_scopes=leaf_scopes(),
    )

    assert {asset.scope for asset in result.assets} == {"brand"}
    assert [asset.drive_file_id for asset in result.assets] == ["brand-1", "brand-2"]
    assert drive.metadata_calls[0] == "brand-grandiosa-mujer-inbox"
    assert drive.find_child_folder_calls == []
    assert drive.list_calls == [("brand-grandiosa-mujer-inbox", 10)]


def test_scope_brand_discovers_selected_brand_child_when_configured_folder_is_parent(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    result = run(
        session,
        drive,
        BatchRequest(scope="brand", brand_slug="grandiosa-mujer", max_total=10, max_per_scope=10),
    )

    assert {asset.scope for asset in result.assets} == {"brand"}
    assert [asset.drive_file_id for asset in result.assets] == ["brand-1", "brand-2"]
    assert drive.metadata_calls[0] == "brand-inbox"
    assert drive.find_child_folder_calls == [("brand-inbox", "grandiosa-mujer")]
    assert drive.list_calls == [("brand-grandiosa-mujer-inbox", 10)]


def test_selected_title_does_not_discover_generic_even_when_generic_has_many_files(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 20, "brand": 0, "title": 2})

    result = run(
        session,
        drive,
        BatchRequest(scope="title", title_slug="mi-otra-yo", max_total=10, max_per_scope=10),
    )

    assert [asset.drive_file_id for asset in result.assets] == ["title-1", "title-2"]
    assert drive.list_calls == [("title-mi-otra-yo-inbox", 10)]


def test_selected_brand_does_not_discover_generic(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 20, "brand": 2, "title": 0})

    result = run(
        session,
        drive,
        BatchRequest(scope="brand", brand_slug="grandiosa-mujer", max_total=10, max_per_scope=10),
    )

    assert [asset.drive_file_id for asset in result.assets] == ["brand-1", "brand-2"]
    assert drive.list_calls == [("brand-grandiosa-mujer-inbox", 10)]


def test_max_total_applies_inside_selected_source(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 20, "brand": 0, "title": 5})

    result = run(
        session,
        drive,
        BatchRequest(scope="title", title_slug="mi-otra-yo", max_total=2, max_per_scope=10),
    )

    assert [asset.drive_file_id for asset in result.assets] == ["title-1", "title-2"]
    assert result.counters.files_discovered == 5
    assert result.counters.files_new == 2
    assert drive.list_calls == [("title-mi-otra-yo-inbox", 10)]


def test_batch_without_scope_keeps_global_discovery(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 1, "title": 1})

    result = run(session, drive, BatchRequest(max_total=3, max_per_scope=1))

    assert [asset.scope for asset in result.assets] == ["generic", "brand", "title"]
    assert [folder_id for folder_id, _limit in drive.list_calls] == ["generic-inbox", "brand-inbox", "title-inbox"]


def test_selected_title_requires_title_slug(session: Session) -> None:
    with pytest.raises(ValueError, match="--scope title requires --title"):
        run(session, BatchFakeDrive(), BatchRequest(scope="title"))


def test_selected_brand_requires_brand_slug(session: Session) -> None:
    with pytest.raises(ValueError, match="--scope brand requires --brand"):
        run(session, BatchFakeDrive(), BatchRequest(scope="brand"))


def test_title_selector_without_title_scope_fails(session: Session) -> None:
    with pytest.raises(ValueError, match="--title can only be used with --scope title"):
        run(session, BatchFakeDrive(), BatchRequest(title_slug="mi-otra-yo"))


def test_brand_selector_without_brand_scope_fails(session: Session) -> None:
    with pytest.raises(ValueError, match="--brand can only be used with --scope brand"):
        run(session, BatchFakeDrive(), BatchRequest(brand_slug="grandiosa-mujer"))


def test_unknown_title_selector_does_not_fall_back_to_global(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    with pytest.raises(ValueError, match="configured title inbox is not available"):
        run(session, drive, BatchRequest(scope="title", title_slug="otra-serie"))

    assert drive.find_child_folder_calls == [("title-inbox", "otra-serie"), ("root", "otra-serie")]
    assert drive.list_calls == []


def test_unknown_brand_selector_does_not_fall_back_to_global(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 2, "title": 2})

    with pytest.raises(ValueError, match="configured brand inbox is not available"):
        run(session, drive, BatchRequest(scope="brand", brand_slug="otra-marca"))

    assert drive.find_child_folder_calls == [("brand-inbox", "otra-marca"), ("root", "otra-marca")]
    assert drive.list_calls == []


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


def test_review_approval_rebuilds_target_name_path_and_hash_from_human_topic(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    old_target_name = asset.target_name
    old_target_path = asset.remote_path
    old_plan_hash = asset.plan_hash

    approve_review_asset(
        session,
        ReviewApproval(
            file_id="generic-1",
            primary_theme="transporte",
            primary_topic="conducción urbana en primera persona",
        ),
    )

    assert asset.status == "move_planned"
    assert asset.move_status == "planned"
    assert asset.target_name == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"
    assert asset.remote_path == f"10_genericos/video/lote-0001/{asset.target_name}"
    assert asset.target_name != old_target_name
    assert asset.remote_path != old_target_path
    assert asset.plan_hash != old_plan_hash
    assert asset.target_name.endswith("__16x9__1983e676.mp4")
    assert asset.plan_hash == compute_plan_hash(asset, asset.remote_path, asset.target_name, 1)


def test_revision_manual_never_becomes_reviewed_filename(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=invalid_placeholder_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.move_status = "planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    session.commit()

    results = replan_reviewed_assets(session, apply=False)

    assert results[0].skipped_reason == "no_meaningful_naming_source"
    assert "revision-manual" not in (results[0].target_name_after or "")


def test_primary_topic_none_does_not_produce_placeholder(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = None
    asset.target_name = "stale.mp4"
    asset.remote_path = f"10_genericos/video/lote-0001/{asset.target_name}"
    session.commit()

    result = replan_reviewed_assets(session, apply=False)[0]

    assert result.skipped_reason is None
    assert result.target_name_after == "urbano-desde-un-auto__16x9__1983e676.mp4"
    assert "revision-manual" not in result.target_name_after


def test_primary_topic_revision_manual_does_not_produce_placeholder(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "revision manual"
    session.commit()

    result = replan_reviewed_assets(session, apply=False)[0]

    assert result.skipped_reason is None
    assert result.target_name_after == "urbano-desde-un-auto__16x9__1983e676.mp4"
    assert "revision-manual" not in result.target_name_after


def test_descriptive_spanish_target_is_not_degraded_to_generic_topic(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    existing = "vista-desde-el-interior-de-un-automovil-conduciendo-por-un-tunel-nocturno__16x9__1983e676.mp4"
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "conduccion nocturna"
    asset.target_name = existing
    asset.remote_path = f"10_genericos/video/lote-0001/{existing}"
    session.commit()

    result = replan_reviewed_assets(session, apply=False)[0]

    assert result.target_name_after == existing


def test_human_descriptive_topic_replaces_old_english_target(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "conducción urbana en primera persona"
    session.commit()

    result = replan_reviewed_assets(session, apply=False)[0]

    assert result.target_name_after == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"


def test_reviewed_replan_preserves_stable_hash_suffix(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "conducción urbana en primera persona"
    session.commit()

    result = replan_reviewed_assets(session, apply=False)[0]

    assert result.target_name_after.endswith("__16x9__1983e676.mp4")


def test_apply_after_review_uses_rebuilt_plan_without_ai(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    approve_review_asset(
        session,
        ReviewApproval(file_id="generic-1", primary_theme="transporte", primary_topic="conducción urbana en primera persona"),
    )

    def forbidden(*_args):
        raise AssertionError("AI must not be called for persisted plan apply")

    applied = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), forbidden)

    assert applied.counters.files_applied == 1
    assert drive.moves == [
        ("generic-1", "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4", "root/10_genericos/video/lote-0001")
    ]
    assert drive.files["generic-1"].id == "generic-1"
    assert drive.files["generic-1"].name == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"


def test_replan_reviewed_dry_run_does_not_modify_db(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "conducción urbana en primera persona"
    old_name = asset.target_name
    old_path = asset.remote_path
    old_hash = asset.plan_hash
    session.commit()

    results = replan_reviewed_assets(session, apply=False)

    assert len(results) == 1
    assert results[0].changed is True
    session.refresh(asset)
    assert asset.target_name == old_name
    assert asset.remote_path == old_path
    assert asset.plan_hash == old_hash


def test_replan_reviewed_apply_updates_only_planned_reviewed_assets(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=2), ai_enricher=english_driving_ai)
    reviewed = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    unreviewed = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2"))
    assert reviewed is not None
    assert unreviewed is not None
    reviewed.status = "move_planned"
    reviewed.needs_human_review = False
    reviewed.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    reviewed.primary_topic = "conducción urbana en primera persona"
    unreviewed.status = "move_planned"
    unreviewed.needs_human_review = False
    unreviewed.reviewed_at = None
    unreviewed.primary_topic = "conducción urbana en primera persona"
    old_unreviewed_name = unreviewed.target_name
    session.commit()

    results = replan_reviewed_assets(session, apply=True)

    assert [result.file_id for result in results] == ["generic-1"]
    assert reviewed.target_name == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"
    assert unreviewed.target_name == old_unreviewed_name


def test_replan_reviewed_apply_does_not_touch_drive(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.status = "move_planned"
    asset.needs_human_review = False
    asset.reviewed_at = datetime(2026, 8, 4, tzinfo=UTC)
    asset.primary_topic = "conducción urbana en primera persona"
    session.commit()

    replan_reviewed_assets(session, apply=True)

    assert drive.moves == []


def test_rename_reviewed_moved_asset_preserves_drive_id_and_parent(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    approve_review_asset(
        session,
        ReviewApproval(file_id="generic-1", primary_theme="transporte", primary_topic="conducción urbana en primera persona"),
    )
    run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai_enricher=ai())
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    parent = drive.files["generic-1"].parents[0]
    drive.files["generic-1"] = DriveFile(
        id="generic-1",
        name="someone-is-driving-a-car-on-a-city-street-with-a-blue__16x9__1983e676.mp4",
        mime_type="video/mp4",
        parents=[parent],
        size=101,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    asset.filename = drive.files["generic-1"].name
    asset.target_name = drive.files["generic-1"].name
    asset.remote_path = f"10_genericos/video/lote-0001/{asset.target_name}"
    asset.source_path = asset.remote_path
    asset.plan_hash = compute_plan_hash(asset, asset.remote_path, asset.target_name, 1)
    session.commit()

    dry = rename_reviewed_moved_asset(session, drive, file_id="generic-1", apply=False)
    applied = rename_reviewed_moved_asset(session, drive, file_id="generic-1", apply=True)

    assert dry.changed is True
    assert applied.drive_file_id_before == "generic-1"
    assert applied.drive_file_id_after == "generic-1"
    assert applied.parent_before == parent
    assert applied.parent_after == parent
    assert drive.files["generic-1"].id == "generic-1"
    assert drive.files["generic-1"].parents == [parent]
    assert drive.files["generic-1"].name == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"
    assert asset.filename == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"


def test_rename_reviewed_moved_dry_run_does_not_touch_drive(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    approve_review_asset(
        session,
        ReviewApproval(file_id="generic-1", primary_theme="transporte", primary_topic="conducción urbana en primera persona"),
    )
    run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai_enricher=ai())
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.target_name = "someone-is-driving-a-car-on-a-city-street-with-a-blue__16x9__1983e676.mp4"
    asset.filename = asset.target_name
    asset.remote_path = f"10_genericos/video/lote-0001/{asset.target_name}"
    session.commit()

    result = rename_reviewed_moved_asset(session, ForbiddenDrive(), file_id="generic-1", apply=False)

    assert result.changed is True
    assert result.target_name_after == "conduccion-urbana-en-primera-persona__16x9__1983e676.mp4"


def test_rename_reviewed_moved_drive_failure_leaves_db_unchanged(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)
    approve_review_asset(
        session,
        ReviewApproval(file_id="generic-1", primary_theme="transporte", primary_topic="conducción urbana en primera persona"),
    )
    run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai_enricher=ai())
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    parent = drive.files["generic-1"].parents[0]
    stale_name = "someone-is-driving-a-car-on-a-city-street-with-a-blue__16x9__1983e676.mp4"
    drive.files["generic-1"] = DriveFile(
        id="generic-1",
        name=stale_name,
        mime_type="video/mp4",
        parents=[parent],
        size=101,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    asset.filename = stale_name
    asset.target_name = stale_name
    asset.remote_path = f"10_genericos/video/lote-0001/{stale_name}"
    asset.source_path = asset.remote_path
    asset.plan_hash = compute_plan_hash(asset, asset.remote_path, asset.target_name, 1)
    session.commit()
    drive.fail_move = True

    with pytest.raises(RuntimeError):
        rename_reviewed_moved_asset(session, drive, file_id="generic-1", apply=True)

    session.refresh(asset)
    assert asset.filename == stale_name
    assert asset.target_name == stale_name
    assert drive.files["generic-1"].name == stale_name


def test_approve_review_asset_does_not_call_drive_or_ai(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.managed_drive_pilot as pilot

    monkeypatch.setattr(pilot, "drive_client_from_environment", lambda: pytest.fail("Drive called"))
    monkeypatch.setattr(pilot, "call_nvidia_vision", lambda *_args, **_kwargs: pytest.fail("NVIDIA called"))
    monkeypatch.setattr(pilot, "call_openai_vision", lambda *_args, **_kwargs: pytest.fail("OpenAI called"))
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive, ai_enricher=english_driving_ai)

    row = approve_review_asset(
        session,
        ReviewApproval(file_id="generic-1", primary_theme="transporte", primary_topic="conducción urbana en primera persona"),
    )

    assert row.status == "move_planned"


def test_real_reviewed_placeholder_cases_with_only_tags_are_skipped(session: Session) -> None:
    cases = [
        (
            "13YRlaNFyIXpl06903yGYf2QntTu2oqRU",
            "134_someone_is_driving_a_car_on_a_city_street_with_a_blue.mp4",
            "someone-is-driving-a-car-on-a-city-street-with-a-blue__16x9__0f0d466f.mp4",
            ["manejando", "automovil", "chofer"],
            "moved",
        ),
        (
            "13CCDMefXkHwg3qx281F3Pehzlq1flYoA",
            "126_someone_is_driving_a_car_with_a_steering_wheel_and_a_red.mp4",
            "someone-is-driving-a-car-with-a-steering-wheel-and-a-red__16x9__53be5303.mp4",
            ["manjeando", "chofer"],
            "planned",
        ),
        (
            "1FjYxzULeXtnUpLvptIs0MLDf7_U6VWDt",
            "038_there_are_a_group_of_people_sitting_at_a_kitchen_table_eating.mp4",
            "there-are-a-group-of-people-sitting-at-a-kitchen-table-eating__16x9__143b334d.mp4",
            ["familia", "cocina", "niños"],
            "planned",
        ),
    ]
    drive = BatchFakeDrive({"generic": 0, "brand": 0, "title": 0})
    for file_id, original_name, target_name, tags, move_status in cases:
        asset = add_reviewed_placeholder_asset(session, file_id, original_name, target_name, tags, move_status)
        if move_status == "moved":
            drive.files[file_id] = DriveFile(
                id=file_id,
                name=target_name,
                mime_type="video/mp4",
                parents=[asset.target_parent_id or "root/10_genericos/video/lote-0014"],
                size=101,
                modified_time=datetime(2026, 8, 3, tzinfo=UTC),
                capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
            )

    replan_results = {
        result.file_id: result
        for result in replan_reviewed_assets(session, apply=False, limit=10)
    }
    rename_result = rename_reviewed_moved_asset(
        session,
        None,
        file_id="13YRlaNFyIXpl06903yGYf2QntTu2oqRU",
        apply=False,
    )

    assert rename_result.skipped_reason == "no_meaningful_naming_source"
    assert rename_result.target_name_after is None
    for file_id, _original_name, _target_name, _tags, move_status in cases:
        if move_status == "moved":
            continue
        result = replan_results[file_id]
        assert result.skipped_reason == "no_meaningful_naming_source"
        assert result.target_name_after is None


def test_reviewed_tags_do_not_become_filename(session: Session) -> None:
    asset = add_reviewed_placeholder_asset(
        session,
        "tag-only-driving",
        "source.mp4",
        "someone-is-driving-a-car__16x9__2b74a063.mp4",
        ["manejando", "automovil", "chofer"],
        "planned",
    )

    result = replan_reviewed_assets(session, apply=False, only_file_id=asset.drive_file_id)[0]

    assert result.skipped_reason == "no_meaningful_naming_source"
    assert result.target_name_after is None


def test_reviewed_family_kitchen_tags_do_not_become_filename(session: Session) -> None:
    asset = add_reviewed_placeholder_asset(
        session,
        "tag-only-family",
        "source.mp4",
        "there-are-a-group-of-people-eating__16x9__1b8f5832.mp4",
        ["familia", "cocina", "ninos"],
        "planned",
    )

    result = replan_reviewed_assets(session, apply=False, only_file_id=asset.drive_file_id)[0]

    assert result.skipped_reason == "no_meaningful_naming_source"
    assert result.target_name_after is None


def add_reviewed_placeholder_asset(
    session: Session,
    file_id: str,
    original_name: str,
    target_name: str,
    tags: list[str],
    move_status: str,
) -> Asset:
    source = session.scalar(select(Source).where(Source.source_id == "managed-drive-pilot-test").limit(1))
    if source is None:
        source = Source(source_id="managed-drive-pilot-test", provider="google_drive", label="Managed Drive Pilot")
        session.add(source)
        session.flush()
    status = "ready" if move_status == "moved" else "move_planned"
    asset = Asset(
        asset_uid=f"drive-{short_id(file_id)}",
        source=source,
        provider="google_drive",
        remote_path=f"10_genericos/video/lote-0014/{target_name}",
        source_path=f"10_genericos/video/lote-0014/{target_name}",
        drive_file_id=file_id,
        remote_file_id=file_id,
        filename=target_name if move_status == "moved" else original_name,
        original_name=original_name,
        original_parent_id="generic-inbox",
        target_parent_id="root/10_genericos/video/lote-0014",
        target_name=target_name,
        status=status,
        move_status=move_status,
        scope="generic",
        type="video",
        mime_type="video/mp4",
        orientation="horizontal-16x9",
        primary_theme="otros",
        primary_topic="revision manual",
        reviewed_at=datetime(2026, 8, 9, tzinfo=UTC),
        reviewed_by="admin",
        path_layout_version="compact_v2",
        plan_version="managed_drive_plan_v2",
    )
    session.add(asset)
    session.flush()
    result_json = {
        "title_es": original_name,
        "description_es": f"Respuesta NVIDIA invalida para {original_name}.",
        "primary_theme": "otros",
        "primary_topic": "revision manual",
        "subject": None,
        "action": None,
        "context": None,
        "tags": tags,
        "suggested_uses": ["revision"],
        "requires_review": True,
        "warnings": ["ai_response_invalid"],
        "ai_analysis_mode": "REAL",
        "frames_analyzed": 0,
        "human_review_overrides_ai": True,
        "reviewed_by": "admin",
    }
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="human-review",
            provider="human",
            input_type="manual",
            prompt_version="human_review_v1",
            result_json=result_json,
        )
    )
    asset.plan_hash = compute_plan_hash(asset, asset.remote_path, asset.target_name, 14)
    session.commit()
    return asset


def test_apply_does_not_analyze_or_register_new_files(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    drive.list_calls.clear()
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
    assert applied.counters.plans_selected == 1
    assert applied.counters.files_discovered == 0
    assert applied.counters.files_analyzed == 0
    assert applied.counters.nvidia_requests == 0
    assert applied.counters.openai_requests == 0
    assert applied.counters.pending_analysis == 0
    assert [asset.drive_file_id for asset in applied.assets] == ["generic-1"]
    assert session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2")) is None
    assert drive.files["generic-2"].parents == ["generic-inbox"]
    assert drive.files["generic-2"].name == "generic-2.mp4"
    assert drive.list_calls == []


def test_apply_global_selects_planned_from_db_without_discovery(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.managed_drive_pilot as pilot

    drive = BatchFakeDrive({"generic": 2, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=2), ai())
    drive.files["generic-3"] = DriveFile(
        id="generic-3",
        name="generic-3.mp4",
        mime_type="video/mp4",
        parents=["generic-inbox"],
        size=103,
        modified_time=datetime(2026, 8, 3, tzinfo=UTC),
        capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
    )
    drive.list_calls.clear()
    monkeypatch.setattr(pilot, "preflight_drive_access", lambda *_args, **_kwargs: pytest.fail("preflight must not run"))

    applied = run(session, drive, BatchRequest(apply=True, max_total=2), ai_enricher=lambda *_args: pytest.fail("AI called"))

    assert applied.counters.files_discovered == 0
    assert applied.counters.pending_analysis == 0
    assert applied.counters.plans_selected == 2
    assert applied.counters.files_applied == 2
    assert {move[0] for move in drive.moves} == {"generic-1", "generic-2"}
    assert session.scalar(select(Asset).where(Asset.drive_file_id == "generic-3")) is None
    assert drive.list_calls == []


def test_apply_only_file_id_uses_db_even_when_not_discovered(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 2, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=2), ai())
    drive.list_calls.clear()

    applied = run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-2",)), ai_enricher=lambda *_args: pytest.fail("AI called"))

    first = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    second = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-2"))
    assert applied.counters.plans_selected == 1
    assert applied.counters.files_applied == 1
    assert first.move_status == "planned"
    assert second.move_status == "moved"
    assert [move[0] for move in drive.moves] == ["generic-2"]
    assert drive.list_calls == []


def test_apply_global_respects_max_total_over_persisted_plans(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 3, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=3, max_per_scope=3), ai())
    drive.list_calls.clear()

    applied = run(session, drive, BatchRequest(apply=True, max_total=2, max_per_scope=3), ai())

    assert applied.counters.plans_selected == 2
    assert applied.counters.files_applied == 2
    assert len(drive.moves) == 2
    assert drive.list_calls == []


def test_apply_global_respects_max_per_scope_over_persisted_plans(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 3, "brand": 2, "title": 1})
    run(session, drive, BatchRequest(max_total=6, max_per_scope=3), ai())
    drive.list_calls.clear()

    applied = run(session, drive, BatchRequest(apply=True, max_total=4, max_per_scope=1), ai())

    moved_by_scope = {
        scope: [row.drive_file_id for row in applied.assets if row.scope == scope]
        for scope in {row.scope for row in applied.assets}
    }
    assert applied.counters.plans_selected == 3
    assert applied.counters.files_applied == 3
    assert len(moved_by_scope.get("generic", [])) == 1
    assert len(moved_by_scope.get("brand", [])) == 1
    assert len(moved_by_scope.get("title", [])) == 1
    assert drive.list_calls == []


def test_apply_global_handles_generic_brand_title_from_db(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 1, "title": 1})
    run(session, drive, BatchRequest(max_total=3, max_per_scope=1), ai())
    drive.list_calls.clear()

    applied = run(session, drive, BatchRequest(apply=True, max_total=3, max_per_scope=1), ai())

    assert {row.scope for row in applied.assets} == {"generic", "brand", "title"}
    assert applied.counters.files_applied == 3
    assert drive.list_calls == []


def test_apply_selected_title_uses_db_driven_candidates_without_discovery(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 1, "title": 1})
    run(session, drive, BatchRequest(max_total=3, max_per_scope=1), ai())
    drive.list_calls.clear()

    applied = run(
        session,
        drive,
        BatchRequest(apply=True, scope="title", title_slug="mi-otra-yo", max_total=3, max_per_scope=3),
        ai_enricher=lambda *_args: pytest.fail("AI called"),
    )

    assert [row.drive_file_id for row in applied.assets] == ["title-1"]
    assert applied.counters.plans_selected == 1
    assert applied.counters.files_applied == 1
    assert [move[0] for move in drive.moves] == ["title-1"]
    assert drive.list_calls == []


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


@pytest.mark.parametrize("primary_topic", ["conduccion", "videojuegos", "futbol", "belleza"])
def test_nvidia_reduced_schema_accepts_one_word_primary_topic(primary_topic: str) -> None:
    result = parse_reduced_result(
        f"""
        {{
          "title_es": "asset visual",
          "description_es": "Asset visual valido.",
          "primary_theme": "otros",
          "primary_topic": "{primary_topic}",
          "tags": ["asset", "visual"],
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
        }}
        """
    )

    assert result.primary_topic == primary_topic


def test_nvidia_reduced_schema_rejects_empty_primary_topic() -> None:
    with pytest.raises(Exception, match="primary_topic"):
        parse_reduced_result(
            """
            {
              "title_es": "asset visual",
              "description_es": "Asset visual invalido.",
              "primary_theme": "otros",
              "primary_topic": "",
              "tags": ["asset", "visual"],
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


def test_nvidia_reduced_schema_rejects_primary_topic_over_eight_words() -> None:
    with pytest.raises(Exception, match="primary_topic"):
        parse_reduced_result(
            """
            {
              "title_es": "asset visual",
              "description_es": "Asset visual invalido.",
              "primary_theme": "otros",
              "primary_topic": "uno dos tres cuatro cinco seis siete ocho nueve",
              "tags": ["asset", "visual"],
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


def test_apply_invalid_plan_hash_blocks_before_mutation(session: Session) -> None:
    drive = BatchFakeDrive({"generic": 1, "brand": 0, "title": 0})
    run(session, drive)
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == "generic-1"))
    assert asset is not None
    asset.primary_topic = "topic changed after plan"
    session.commit()
    drive.list_calls.clear()

    with pytest.raises(RuntimeError, match="plan hash mismatch"):
        run(session, drive, BatchRequest(apply=True, only_file_ids=("generic-1",)), ai())

    session.refresh(asset)
    assert asset.move_status == "planned"
    assert asset.status == "move_planned"
    assert drive.moves == []
    assert drive.created_folders == []
    assert drive.list_calls == []


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
    assert second.counters.pending_analysis == 0
    assert second.counters.drive_mutations_attempted == 0
    assert second.counters.drive_mutations_succeeded == 0
    assert folder_row.item_count == 1


def test_status_filter_represents_all_planned_assets(session: Session) -> None:
    from app.services.managed_drive_pilot import list_drive_status

    drive = BatchFakeDrive({"generic": 4, "brand": 0, "title": 0})
    run(session, drive, BatchRequest(max_total=4, max_per_scope=4), ai())

    status = list_drive_status(session, status="move_planned", limit=10)

    assert status.summary["planned"] == 4
    assert {row.drive_file_id for row in status.rows} == {"generic-1", "generic-2", "generic-3", "generic-4"}


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
