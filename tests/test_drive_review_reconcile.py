from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, AssetAIAnalysis, ManagedDriveFolder, Source
from app.services.managed_drive.contracts import DriveFile
from app.services.managed_drive.layout import compact_folder_primary_topic
from app.services.managed_drive_pilot import reconcile_rollback_required_assets
from scripts import asset_hub


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


class ReconcileDrive:
    def __init__(self) -> None:
        self.files: dict[str, DriveFile] = {}
        self.metadata_calls: list[str] = []
        self.verify_calls: list[tuple[str, str, str]] = []
        self.find_child_calls: list[tuple[str, str]] = []
        self.mutation_calls = 0
        self.fail_ids: set[str] = set()
        self.root_folder_ids: tuple[str, ...] = ()

    def get_file_metadata(self, file_id: str) -> DriveFile:
        self.metadata_calls.append(file_id)
        if file_id in self.fail_ids:
            raise RuntimeError("invalid_grant recovered but this read failed")
        return self.files[file_id]

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        self.verify_calls.append((file_id, expected_name, expected_parent_id))
        current = self.files[file_id]
        return current.name == expected_name and current.parents == [expected_parent_id] and not current.trashed

    def find_child_folder_id(self, parent_id: str, name: str) -> str | None:
        self.find_child_calls.append((parent_id, name))
        return None

    def rename_and_move_file(self, *_args, **_kwargs) -> DriveFile:
        self.mutation_calls += 1
        raise AssertionError("reconcile must not move Drive files")

    def restore_file_location(self, *_args, **_kwargs) -> DriveFile:
        self.mutation_calls += 1
        raise AssertionError("reconcile must not restore Drive files")


def drive_file(file_id: str, name: str, parent: str, *, trashed: bool = False) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="video/mp4",
        parents=[parent],
        size=100,
        modified_time=datetime(2026, 8, 10, tzinfo=UTC),
        trashed=trashed,
    )


def source(session: Session) -> Source:
    existing = session.scalar(select(Source).where(Source.source_id == "managed-drive-pilot-test"))
    if existing is not None:
        return existing
    created = Source(source_id="managed-drive-pilot-test", provider="google_drive", label="Managed Drive Pilot Test")
    session.add(created)
    session.flush()
    return created


def rollback_asset(
    session: Session,
    file_id: str,
    *,
    status: str = "review_required",
    move_status: str = "rollback_required",
    review_reason: str | None = "move_error; rollback_error: invalid_grant",
    target_parent_id: str | None = "target-parent",
    target_name: str | None = None,
) -> Asset:
    resolved_target_name = target_name if target_name is not None else f"{file_id}-target.mp4"
    asset = Asset(
        asset_uid=f"asset-{file_id}",
        source=source(session),
        provider="google_drive",
        remote_path=f"10_genericos/video/lote-0001/{file_id}-target.mp4",
        source_path=f"10_genericos/video/lote-0001/{file_id}-target.mp4",
        drive_file_id=file_id,
        remote_file_id=file_id,
        filename=f"{file_id}-source.mp4",
        original_name=f"{file_id}-source.mp4",
        original_parent_id="source-parent",
        target_name=resolved_target_name,
        target_parent_id=target_parent_id,
        type="video",
        mime_type="video/mp4",
        scope="generic",
        status=status,
        move_status=move_status,
        needs_human_review=True,
        review_reason=review_reason,
        primary_theme="personas",
        primary_topic="caminar",
        ai_model="existing-model",
        ai_enrichment_status="ready",
        path_layout_version="compact_v3",
        plan_hash=f"hash-{file_id}",
    )
    session.add(asset)
    session.flush()
    return asset


def managed_folder(session: Session, folder_id: str, asset: Asset) -> ManagedDriveFolder:
    folder = ManagedDriveFolder(
        drive_folder_id=folder_id,
        root_folder_id="root-folder",
        scope=asset.scope or "generic",
        brand_id=asset.brand_id,
        title_slug=asset.title_slug,
        title_type=asset.title_type,
        collection=asset.collection,
        media_type=asset.type or "video",
        path_layout_version=asset.path_layout_version,
        context_slug=None,
        primary_theme=None,
        primary_topic=compact_folder_primary_topic(asset),
        orientation="compact",
        batch_number=1,
    )
    session.add(folder)
    session.flush()
    return folder


def test_reconcile_dry_run_does_not_change_drive_or_db(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    snapshot = (asset.status, asset.move_status, asset.needs_human_review, asset.review_reason)
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)
    session.refresh(asset)

    assert result[0].reconciliation == "already_at_target"
    assert (asset.status, asset.move_status, asset.needs_human_review, asset.review_reason) == snapshot
    assert drive.mutation_calls == 0
    assert drive.verify_calls == []


def test_reconcile_selects_only_review_required_rollback_required(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "repair-me")
    rollback_asset(session, "normal-review", move_status="planned", review_reason="classification_ambiguous")
    rollback_asset(session, "ready-one", status="ready", move_status="moved", review_reason=None)
    drive.files["repair-me"] = drive_file("repair-me", "repair-me-source.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)

    assert [item.drive_file_id for item in result] == ["repair-me"]
    assert drive.metadata_calls == ["repair-me"]


def test_reconcile_already_at_target_proposes_finalize_moved(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]

    assert result.reconciliation == "already_at_target"
    assert result.action == "finalize_moved"


def test_reconcile_still_at_source_proposes_reset_planned(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]

    assert result.reconciliation == "still_at_source"
    assert result.action == "reset_planned"


def test_reconcile_still_at_source_without_target_parent_proposes_reset_planned(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1", target_parent_id=None)
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]

    assert result.reconciliation == "still_at_source"
    assert result.action == "reset_planned"
    assert result.reason is None
    assert drive.find_child_calls == []


def test_reconcile_still_at_source_does_not_require_target_name(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1", target_parent_id=None, target_name="")
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]

    assert result.reconciliation == "still_at_source"
    assert result.action == "reset_planned"


def test_reconcile_unexpected_location_requires_manual(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "somewhere-else")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]

    assert result.reconciliation == "manual_required"
    assert result.action == "none"
    assert "expected source or target" in (result.reason or "")


def test_reconcile_apply_already_at_target_sets_ready_moved(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=True)[0]
    session.refresh(asset)

    assert result.action == "finalize_moved"
    assert asset.status == "ready"
    assert asset.move_status == "moved"
    assert asset.needs_human_review is False


def test_reconcile_apply_still_at_source_sets_move_planned(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=True)[0]
    session.refresh(asset)

    assert result.action == "reset_planned"
    assert asset.status == "move_planned"
    assert asset.move_status == "planned"
    assert asset.needs_human_review is False
    assert asset.review_reason is None
    assert drive.mutation_calls == 0


def test_reconcile_apply_still_at_source_without_target_parent_updates_only_db(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1", target_parent_id=None)
    target_path = asset.remote_path
    target_name = asset.target_name
    plan_hash = asset.plan_hash
    drive.files["file-1"] = drive_file("file-1", "current-source-name.mp4", "source-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=True)[0]
    session.refresh(asset)

    assert result.reconciliation == "still_at_source"
    assert result.action == "reset_planned"
    assert asset.status == "move_planned"
    assert asset.move_status == "planned"
    assert asset.needs_human_review is False
    assert asset.review_reason is None
    assert asset.drive_file_id == "file-1"
    assert asset.remote_path == target_path
    assert asset.target_name == target_name
    assert asset.plan_hash == plan_hash
    assert drive.mutation_calls == 0
    assert drive.verify_calls == []


def test_reconcile_manual_required_stays_intact(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "unexpected-parent", trashed=True)
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=True)[0]
    session.refresh(asset)

    assert result.reconciliation == "manual_required"
    assert asset.status == "review_required"
    assert asset.move_status == "rollback_required"
    assert asset.needs_human_review is True


def test_reconcile_preserves_drive_file_id(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    session.commit()

    reconcile_rollback_required_assets(session, drive, apply=True)
    session.refresh(asset)

    assert asset.drive_file_id == "file-1"


def test_reconcile_preserves_ai_metadata_and_target_path(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="kept-model",
            provider="nvidia",
            input_type="preview",
            prompt_version="v1",
            result_json={"tags": ["kept"]},
            confidence=0.9,
        )
    )
    target_path = asset.remote_path
    drive.files["file-1"] = drive_file("file-1", "file-1-source.mp4", "source-parent")
    session.commit()

    reconcile_rollback_required_assets(session, drive, apply=True)
    session.refresh(asset)
    analysis = session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset.id))

    assert asset.remote_path == target_path
    assert asset.target_name == "file-1-target.mp4"
    assert asset.ai_model == "existing-model"
    assert analysis is not None
    assert analysis.result_json == {"tags": ["kept"]}


def test_reconcile_already_at_target_resolves_missing_target_parent_read_only(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1", target_parent_id=None)
    managed_folder(session, "resolved-target-parent", asset)
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "resolved-target-parent")
    session.commit()

    result = reconcile_rollback_required_assets(session, drive, apply=False)[0]
    session.refresh(asset)

    assert result.reconciliation == "already_at_target"
    assert result.action == "finalize_moved"
    assert asset.target_parent_id is None
    assert drive.mutation_calls == 0


def test_reconcile_does_not_call_nvidia_or_openai(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    session.commit()

    monkeypatch.setattr("app.services.managed_drive_pilot.call_nvidia_vision", lambda *_args, **_kwargs: pytest.fail("NVIDIA called"))
    monkeypatch.setattr("app.services.managed_drive_pilot.call_openai_vision", lambda *_args, **_kwargs: pytest.fail("OpenAI called"))

    reconcile_rollback_required_assets(session, drive, apply=True)


def test_reconcile_leaves_normal_reviews_intact(session: Session) -> None:
    drive = ReconcileDrive()
    normal = rollback_asset(session, "normal-review", move_status="planned", review_reason="classification_ambiguous")
    rollback_asset(session, "repair-me")
    drive.files["repair-me"] = drive_file("repair-me", "repair-me-target.mp4", "target-parent")
    session.commit()

    reconcile_rollback_required_assets(session, drive, apply=True)
    session.refresh(normal)

    assert normal.status == "review_required"
    assert normal.move_status == "planned"
    assert normal.review_reason == "classification_ambiguous"


def test_reconcile_api_error_does_not_abort_remaining_assets(session: Session) -> None:
    drive = ReconcileDrive()
    rollback_asset(session, "bad-file")
    good = rollback_asset(session, "good-file")
    drive.fail_ids.add("bad-file")
    drive.files["good-file"] = drive_file("good-file", "good-file-target.mp4", "target-parent")
    session.commit()

    results = reconcile_rollback_required_assets(session, drive, apply=True)
    session.refresh(good)

    assert [item.drive_file_id for item in results] == ["good-file", "bad-file"]
    assert any(item.reconciliation == "manual_required" and item.drive_file_id == "bad-file" for item in results)
    assert good.status == "ready"
    assert good.move_status == "moved"


def test_reconcile_is_idempotent_after_repair(session: Session) -> None:
    drive = ReconcileDrive()
    asset = rollback_asset(session, "file-1")
    drive.files["file-1"] = drive_file("file-1", "file-1-target.mp4", "target-parent")
    session.commit()

    first = reconcile_rollback_required_assets(session, drive, apply=True)
    second = reconcile_rollback_required_assets(session, drive, apply=True)
    session.refresh(asset)

    assert len(first) == 1
    assert second == []
    assert asset.status == "ready"
    assert asset.move_status == "moved"


def test_reconcile_cli_outputs_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PILOT_DATABASE_URL=sqlite:///:memory:\n", encoding="utf-8")
    result = reconcile_rollback_required_assets
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def fake_reconcile(_session, _drive_client, *, apply: bool, limit: int):
        assert apply is False
        assert limit == 59
        drive = ReconcileDrive()
        rollback_asset(_session, "file-1")
        _session.commit()
        metadata = drive_file("file-1", "file-1-target.mp4", "target-parent")
        drive.files["file-1"] = metadata
        return result(_session, drive, apply=False, limit=500)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: factory)
    monkeypatch.setattr(asset_hub, "drive_client_from_environment", lambda: object())
    monkeypatch.setattr(asset_hub, "reconcile_rollback_required_assets", fake_reconcile)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "review", "reconcile", "--limit", "59"])
    output = capsys.readouterr().out

    assert code == 0
    assert "ROLLBACK_REQUIRED_MATCHED=1" in output
    assert "ALREADY_AT_TARGET=1" in output
    assert "DRIVE_MUTATIONS=0" in output
