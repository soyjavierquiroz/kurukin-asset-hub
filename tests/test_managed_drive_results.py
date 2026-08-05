from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services import managed_drive_pilot
from app.services import managed_drive
from app.services.managed_drive import results


MOVED_RESULTS = (
    "MutationStats",
    "PreflightResult",
    "IngestPlan",
    "BatchAssetResult",
    "BatchCounters",
    "BatchResult",
    "LegacyLayoutAssetPlan",
    "LegacyLayoutMigrationResult",
    "DriveStatusRow",
    "DriveStatusResult",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = PROJECT_ROOT / "app/services/managed_drive_pilot.py"
RESULTS_PATH = PROJECT_ROOT / "app/services/managed_drive/results.py"


def class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def sample_ingest_plan(plan_created_at: datetime | None = None) -> results.IngestPlan:
    return results.IngestPlan(
        drive_file_id="file-1",
        original_name="Hero.mov",
        original_parent_id="inbox",
        source_drive_id="drive-1",
        scope="generic",
        mime_type="video/quicktime",
        media_type="video",
        width=1920,
        height=1080,
        duration=12.25,
        aspect_ratio=1.777,
        orientation="horizontal-16x9",
        title_es="Plano hero",
        description_es="Descripcion",
        primary_theme="tecnologia",
        primary_topic="software",
        suggested_tags=["hero", "producto"],
        suggested_uses=["web"],
        can_flip_horizontal=True,
        flip_risk_reasons=[],
        can_zoom=True,
        max_safe_zoom=1.2,
        has_visible_text=False,
        has_logo=True,
        generic_compatibility=True,
        target_path="tecnologia/software/16x9",
        target_name="hero.mov",
        target_batch=3,
        path_layout_version="compact_v2",
        preview_path="/tmp/preview.jpg",
        thumbnail_path="/tmp/thumb.jpg",
        classification_confidence=0.93,
        warnings=["low confidence"],
        tags_source="ai",
        requires_review=True,
        drive_mutations_performed=2,
        mutation_stats={"mutations_attempted": 2},
        simulated_update={"status": "ready"},
        plan_version="managed_drive_plan_v2",
        plan_created_at=plan_created_at,
        plan_hash="hash-1",
        database_status="ready",
        brand="kurukin",
        title="Hero",
        collection="evergreen",
        asset_id=42,
        status="ready",
        move_status="moved",
        ai_analysis_mode="NVIDIA",
        ai_provider="nvidia",
        ai_model="model-1",
        frames_analyzed=4,
        thumbnail_valid=True,
        thumbnail_size=1234,
        result="applied",
        ai_call=True,
    )


def test_results_are_reexported_from_pilot_and_package_with_identity() -> None:
    for name in MOVED_RESULTS:
        assert getattr(managed_drive_pilot, name) is getattr(results, name)
        assert getattr(managed_drive, name) is getattr(results, name)


def test_pilot_has_no_duplicate_class_definitions_for_moved_results() -> None:
    assert class_names(PILOT_PATH).isdisjoint(MOVED_RESULTS)
    assert set(MOVED_RESULTS) <= class_names(RESULTS_PATH)


def test_mutation_stats_defaults_and_to_dict_are_unchanged_and_independent() -> None:
    first = results.MutationStats()
    second = results.MutationStats()

    first.attempt("rename")
    first.succeed("rename")
    first.compensate("move")
    first.uncompensate("trash")

    assert first.to_dict() == {
        "mutations_attempted": 1,
        "mutations_succeeded": 1,
        "mutations_compensated": 1,
        "mutations_uncompensated": 1,
    }
    assert second.to_dict() == {
        "mutations_attempted": 0,
        "mutations_succeeded": 0,
        "mutations_compensated": 0,
        "mutations_uncompensated": 0,
    }
    assert first.attempted is not second.attempted
    assert first.succeeded is not second.succeeded
    assert first.compensated is not second.compensated
    assert first.uncompensated is not second.uncompensated


def test_preflight_result_defaults_factory_and_lines_are_unchanged() -> None:
    first = results.PreflightResult(
        auth_mode="adc",
        source_folder="OK",
        source_drive="drive-1",
        destination_folder="OK",
        destination_drive="drive-1",
        same_drive=True,
        can_list_source=True,
        can_create_in_destination=True,
        can_move_files=True,
        test_file_id=None,
        destination_inside_pilot_root=True,
        legacy_ids_detected=[],
        scopes_complete=True,
    )
    second = results.PreflightResult(
        auth_mode="adc",
        source_folder="OK",
        source_drive="drive-1",
        destination_folder="OK",
        destination_drive="drive-1",
        same_drive=True,
        can_list_source=True,
        can_create_in_destination=True,
        can_move_files=True,
        test_file_id=None,
        destination_inside_pilot_root=True,
        legacy_ids_detected=[],
        scopes_complete=True,
    )

    first.warnings.append("manual warning")

    assert first.ready_for_dry_run is True
    assert first.warnings == ["manual warning"]
    assert second.warnings == []
    assert first.warnings is not second.warnings
    assert first.to_lines() == [
        "AUTHENTICATION: OK",
        "AUTH_MODE: adc",
        "SOURCE_FOLDER: OK",
        "SOURCE_DRIVE: drive-1",
        "DESTINATION_FOLDER: OK",
        "DESTINATION_DRIVE: drive-1",
        "SAME_DRIVE: YES",
        "CAN_LIST_SOURCE: YES",
        "CAN_CREATE_IN_DESTINATION: YES",
        "CAN_MOVE_FILES: YES",
        "TEST_FILE_ID: NONE",
        "DESTINATION_INSIDE_PILOT_ROOT: YES",
        "LEGACY_IDS_DETECTED: NO",
        "SCOPES_COMPLETE: YES",
        "DRIVE_MUTATIONS_PERFORMED: 0",
        "READY_FOR_DRY_RUN: YES",
        "WARNINGS: manual warning",
        "AUTH_REFRESHABLE=NO",
        "CUSTOM_OAUTH_CLIENT=NO",
        "SOURCE_ACCESSIBLE=NO",
        "ROOT_ACCESSIBLE=NO",
        "FILE_ACCESSIBLE=NO",
        "FILE_TRASHED=NO",
        "CAN_UPDATE=NO",
        "CAN_MOVE_WITHIN_DRIVE=NO",
        "CAN_ADD_CHILDREN=NO",
        "CAN_RESTORE_FROM_TRASH=NO",
        "OAUTH_SCOPE_SUFFICIENT=NO",
    ]


def test_ingest_plan_defaults_factory_and_to_dict_are_unchanged() -> None:
    first = sample_ingest_plan(datetime(2026, 8, 5, 12, 30, 45, tzinfo=UTC))
    second = sample_ingest_plan()

    first.mutation_stats["extra"] = 1

    assert first.mutation_stats is not second.mutation_stats
    assert first.to_dict() == {
        "drive_file_id": "file-1",
        "original_name": "Hero.mov",
        "original_parent_id": "inbox",
        "source_drive_id": "drive-1",
        "scope": "generic",
        "brand": "kurukin",
        "title": "Hero",
        "collection": "evergreen",
        "mime_type": "video/quicktime",
        "media_type": "video",
        "width": 1920,
        "height": 1080,
        "duration": 12.25,
        "aspect_ratio": 1.777,
        "orientation": "horizontal-16x9",
        "title_es": "Plano hero",
        "description_es": "Descripcion",
        "primary_theme": "tecnologia",
        "primary_topic": "software",
        "tags": ["hero", "producto"],
        "tags_source": "ai",
        "suggested_uses": ["web"],
        "can_flip_horizontal": True,
        "flip_risk_reasons": [],
        "can_zoom": True,
        "max_safe_zoom": 1.2,
        "has_visible_text": False,
        "has_logo": True,
        "generic_compatibility": True,
        "target_path": "tecnologia/software/16x9",
        "target_name": "hero.mov",
        "target_batch": 3,
        "path_layout_version": "compact_v2",
        "preview_path": "/tmp/preview.jpg",
        "thumbnail_path": "/tmp/thumb.jpg",
        "classification_confidence": 0.93,
        "warnings": ["low confidence"],
        "requires_review": True,
        "drive_mutations_performed": 2,
        "mutation_stats": {"mutations_attempted": 2, "extra": 1},
        "simulated_update": {"status": "ready"},
        "plan_version": "managed_drive_plan_v2",
        "plan_created_at": "2026-08-05T12:30:45+00:00",
        "plan_hash": "hash-1",
        "database_status": "ready",
        "asset_id": 42,
        "status": "ready",
        "move_status": "moved",
        "ai_analysis_mode": "NVIDIA",
        "ai_provider": "nvidia",
        "ai_model": "model-1",
        "frames_analyzed": 4,
        "thumbnail_valid": True,
        "thumbnail_size": 1234,
        "result": "applied",
        "ai_call": True,
    }
    assert second.to_dict()["plan_created_at"] is None


def test_batch_counters_assets_and_dates_serialize_without_shared_state() -> None:
    started_at = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=1.2345)
    asset = results.BatchAssetResult(
        drive_file_id="file-1",
        scope="generic",
        original_name="Hero.mov",
        provider="nvidia",
        analysis_result="ok",
        target_path="target/path",
        final_status="ready",
        move_status="moved",
        review_reason="ambiguous",
        drive_mutations=2,
        error="none",
        pending_analysis=True,
    )
    first = results.BatchResult(
        run_mode="apply",
        started_at=started_at,
        finished_at=finished_at,
        concurrency=2,
        lock_acquired=True,
        counters=results.BatchCounters(files_discovered=1, nvidia_requests=1),
    )
    second = results.BatchResult(
        run_mode="dry-run",
        started_at=started_at,
        finished_at=finished_at,
        concurrency=1,
        lock_acquired=False,
        counters=results.BatchCounters(),
    )

    first.assets.append(asset)

    assert first.assets is not second.assets
    assert second.assets == []
    assert first.duration_seconds == 1.234
    assert first.to_dict() == {
        "run_mode": "apply",
        "started_at": "2026-08-05T12:00:00+00:00",
        "finished_at": "2026-08-05T12:00:01.234500+00:00",
        "concurrency": 2,
        "lock": "acquired",
        "duration_seconds": 1.234,
        "files_discovered": 1,
        "files_new": 0,
        "files_skipped": 0,
        "files_analyzed": 0,
        "files_applied": 0,
        "files_ready": 0,
        "files_sent_to_review": 0,
        "files_failed": 0,
        "nvidia_requests": 1,
        "nvidia_retries": 0,
        "openai_requests": 0,
        "drive_mutations_attempted": 0,
        "drive_mutations_succeeded": 0,
        "duplicates": 0,
        "pending_analysis": 0,
        "already_applied": 0,
        "assets": [
            {
                "drive_file_id": "file-1",
                "scope": "generic",
                "original_name": "Hero.mov",
                "provider": "nvidia",
                "analysis_result": "ok",
                "target_path": "target/path",
                "final_status": "ready",
                "move_status": "moved",
                "review_reason": "ambiguous",
                "drive_mutations": 2,
                "error": "none",
                "pending_analysis": True,
            }
        ],
    }


def test_legacy_migration_defaults_lists_and_to_dict_are_unchanged() -> None:
    plan = results.LegacyLayoutAssetPlan(
        drive_file_id="file-1",
        status="ready",
        move_status="planned",
        scope="generic",
        current_name="old.mov",
        current_parent_id="old-parent",
        current_path="old/path",
        target_name="new.mov",
        compact_target_path="new/path",
        compact_target_parts=["new", "path"],
        target_parent_id="new-parent",
        trashed=False,
        size=123,
        mime_type="video/mp4",
        name_changed=True,
        would_add_parent="new-parent",
        would_remove_parent="old-parent",
        would_set_trashed_false=False,
        database_updates={"move_status": "moved"},
    )
    first = results.LegacyLayoutMigrationResult(simulated=True)
    second = results.LegacyLayoutMigrationResult(simulated=False)

    first.assets_detected.append("file-1")
    first.cleaned_folder_ids.append("folder-1")
    first.not_deleted_folder_ids.append("folder-2")
    first.plans.append(plan)

    assert first.assets_detected is not second.assets_detected
    assert first.cleaned_folder_ids is not second.cleaned_folder_ids
    assert first.not_deleted_folder_ids is not second.not_deleted_folder_ids
    assert first.plans is not second.plans
    assert second.to_dict()["plans"] == []
    assert first.to_dict() == {
        "simulated": True,
        "assets_detected": ["file-1"],
        "assets_migrated": 0,
        "generic_migrated": 0,
        "brand_migrated": 0,
        "title_migrated": 0,
        "review_migrated": 0,
        "folders_created": 0,
        "folders_deleted": 0,
        "drive_mutations": 0,
        "duplicates": 0,
        "cleaned_folder_ids": ["folder-1"],
        "not_deleted_folder_ids": ["folder-2"],
        "managed_folder_records_deleted": 0,
        "plans": [
            {
                "drive_file_id": "file-1",
                "status": "ready",
                "move_status": "planned",
                "scope": "generic",
                "current_name": "old.mov",
                "current_parent_id": "old-parent",
                "current_path": "old/path",
                "target_name": "new.mov",
                "compact_target_path": "new/path",
                "target_parent_id": "new-parent",
                "trashed": False,
                "size": 123,
                "mimeType": "video/mp4",
                "name_changed": True,
                "would_add_parent": "new-parent",
                "would_remove_parent": "old-parent",
                "would_set_trashed_false": False,
                "database_updates": {"move_status": "moved"},
            }
        ],
    }


def test_drive_status_dates_lists_and_to_dict_are_unchanged() -> None:
    row = results.DriveStatusRow(
        drive_file_id="file-1",
        scope="generic",
        original_name="Hero.mov",
        current_name="hero.mov",
        status="ready",
        move_status="moved",
        requires_review=False,
        review_reason=None,
        provider="nvidia",
        model="model-1",
        target_path="target/path",
        path_layout_version="compact_v2",
        updated_at=datetime(2026, 8, 5, 12, 30, 45, tzinfo=UTC),
    )
    empty_date_row = results.DriveStatusRow(
        drive_file_id=None,
        scope=None,
        original_name=None,
        current_name="manual.mov",
        status="planned",
        move_status="planned",
        requires_review=True,
        review_reason="manual",
        provider=None,
        model=None,
        target_path=None,
        path_layout_version=None,
        updated_at=None,
    )
    first = results.DriveStatusResult(rows=[row], summary={"ready": 1})
    second = results.DriveStatusResult(rows=[], summary={})

    first.rows.append(empty_date_row)

    assert first.rows is not second.rows
    assert first.to_dict() == {
        "summary": {"ready": 1},
        "assets": [
            {
                "drive_file_id": "file-1",
                "scope": "generic",
                "original_name": "Hero.mov",
                "current_name": "hero.mov",
                "status": "ready",
                "move_status": "moved",
                "requires_review": False,
                "review_reason": None,
                "provider": "nvidia",
                "model": "model-1",
                "target_path": "target/path",
                "path_layout_version": "compact_v2",
                "updated_at": "2026-08-05T12:30:45+00:00",
            },
            {
                "drive_file_id": None,
                "scope": None,
                "original_name": None,
                "current_name": "manual.mov",
                "status": "planned",
                "move_status": "planned",
                "requires_review": True,
                "review_reason": "manual",
                "provider": None,
                "model": None,
                "target_path": None,
                "path_layout_version": None,
                "updated_at": None,
            },
        ],
    }
    assert second.to_dict() == {"summary": {}, "assets": []}


def test_results_module_has_no_forbidden_runtime_imports() -> None:
    forbidden_modules = (
        "sqlalchemy",
        "app.models",
        "app.config",
        "app.services.managed_drive_pilot",
        "app.services.ai_asset_enrichment",
        "app.services.ai_prompts",
        "app.services.asset_preview",
        "app.services.ai_providers",
        "app.services.rclone_service",
        "subprocess",
        "urllib",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
    )
    allowed_modules = {
        "__future__",
        "dataclasses",
        "datetime",
        "typing",
        "app.services.managed_drive.contracts",
        "app.services.managed_drive.naming",
    }
    modules = imported_modules(RESULTS_PATH)

    assert modules <= allowed_modules
    assert all(not module.startswith(forbidden_modules) for module in modules)
