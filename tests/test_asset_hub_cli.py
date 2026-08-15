from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, Brand, Source
from app.services.managed_drive_pilot import (
    ReviewApproval,
    approve_review_asset,
    list_drive_status,
)
from scripts import asset_hub


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


def test_cli_help_works_from_other_directory(tmp_path: Path) -> None:
    executable = shutil.which("asset-hub")
    command = [executable or sys.executable, "--help"] if executable else [sys.executable, "-m", "scripts.asset_hub", "--help"]
    result = subprocess.run(command, cwd=tmp_path, check=False, capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert "drive" in result.stdout


def test_batch_dry_run_cli_does_not_apply_or_mutate_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_run(_session, request):
        captured["request"] = request
        return fake_batch_result(apply=False)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", fake_run)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "batch", "--max-total", "50", "--max-per-scope", "25", "--json"])

    assert code == 0
    assert captured["request"].apply is False


def test_batch_apply_cli_does_not_call_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_run(_session, request):
        captured["request"] = request
        return fake_batch_result(apply=True, nvidia_requests=0, openai_requests=0)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", fake_run)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "batch", "--apply", "--quiet"])

    assert code == 0
    assert captured["request"].apply is True


def test_batch_cli_passes_source_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_run(_session, request):
        captured["request"] = request
        return fake_batch_result(apply=False)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", fake_run)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "drive",
            "batch",
            "--scope",
            "title",
            "--title",
            "mi-otra-yo",
            "--quiet",
        ]
    )

    assert code == 0
    assert captured["request"].scope == "title"
    assert captured["request"].title_slug == "mi-otra-yo"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--scope", "title"], "--scope title requires --title"),
        (["--scope", "brand"], "--scope brand requires --brand"),
        (["--title", "mi-otra-yo"], "--title can only be used with --scope title"),
        (["--brand", "grandiosa-mujer"], "--brand can only be used with --scope brand"),
    ],
)
def test_batch_cli_selector_validation_fails_with_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    env_file = write_env(tmp_path)
    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", lambda *_args, **_kwargs: pytest.fail("batch should not run"))

    code = asset_hub.main(["--env-file", str(env_file), "drive", "batch", *arguments])

    captured = capsys.readouterr()
    assert code == 2
    assert message in captured.err


def test_status_filters_correctly(session: Session) -> None:
    source = add_source(session)
    add_asset(session, source, "ready-1", status="ready", scope="generic")
    add_asset(session, source, "planned-1", status="move_planned", scope="brand")
    add_asset(session, source, "planned-2", status="move_planned", scope="generic")

    result = list_drive_status(session, status="move_planned", scope="generic", limit=100)

    assert [row.drive_file_id for row in result.rows] == ["planned-2"]
    assert result.summary["ready"] == 1
    assert result.summary["planned"] == 2


def test_doctor_detects_wrong_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)
    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "current_database_name", lambda _session: "kurukin_asset_hub")
    patch_green_doctor(monkeypatch, skip={"check_current_database", "check_legacy_not_selected"})

    code = asset_hub.main(["--env-file", str(env_file), "drive", "doctor", "--quiet"])

    assert code == 1


def test_doctor_detects_stale_alembic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)
    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    patch_green_doctor(monkeypatch, skip={"check_alembic_head"})
    monkeypatch.setattr(asset_hub, "check_alembic_head", lambda _args, _session: asset_hub.check("alembic_head", False))

    code = asset_hub.main(["--env-file", str(env_file), "drive", "doctor", "--quiet"])

    assert code == 1


def test_doctor_does_not_call_ai_without_check_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)
    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    patch_green_doctor(monkeypatch)
    monkeypatch.setattr(asset_hub, "check_nvidia_model", lambda *_args: pytest.fail("AI check called"))

    code = asset_hub.main(["--env-file", str(env_file), "drive", "doctor", "--quiet"])

    assert code == 0


def test_review_approve_preserves_scope_and_recalculates_compact_v3(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "root")
    from app.config import get_settings

    get_settings.cache_clear()
    source = add_source(session)
    asset = add_asset(session, source, "review-1", status="review_required", scope="brand")
    asset.primary_theme = "otros"
    asset.primary_topic = "revision manual"
    asset.collection = "evergreen"
    session.commit()
    old_hash = asset.plan_hash

    row = approve_review_asset(
        session,
        ReviewApproval(file_id="review-1", primary_theme="personas", primary_topic="bienestar yoga"),
    )

    assert row.scope == "brand"
    assert row.status == "move_planned"
    assert row.move_status == "planned"
    assert row.path_layout_version == "compact_v3"
    assert row.target_path and row.target_path.startswith("20_marcas/")
    assert asset.plan_hash != old_hash


def test_layout_migrate_is_simulation_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_migrate(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "simulated": kwargs["simulate"],
                "assets_detected": [],
                "assets_migrated": 0,
                "folders_created": 0,
                "folders_deleted": 0,
                "drive_mutations": 0,
                "duplicates": 0,
            }
        )

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "drive_client_from_environment", lambda: object())
    monkeypatch.setattr(asset_hub, "migrate_legacy_layout_assets", fake_migrate)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "layout", "migrate", "--from", "legacy", "--to", "compact_v3", "--quiet"])

    assert code == 0
    assert captured["simulate"] is True
    assert captured["cleanup_empty_folders"] is False


def test_layout_migrate_compact_v2_title_scope_is_passed_to_migrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_migrate(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "simulated": kwargs["simulate"],
                "assets_detected": ["file-1"],
                "assets_migrated": 0,
                "folders_created": 0,
                "folders_deleted": 0,
                "drive_mutations": 0,
                "duplicates": 0,
                "plans": [
                    {
                        "drive_file_id": "file-1",
                        "current_path": "30_peliculas_series/series/mi-otra-yo/brolls-generales/video/lote-0001/file.mp4",
                        "compact_target_path": "30_peliculas_series/series/mi-otra-yo/video/lote-0001/file.mp4",
                        "database_updates": {
                            "path_layout_version_before": "compact_v2",
                            "path_layout_version": "compact_v3",
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "drive_client_from_environment", lambda: object())
    monkeypatch.setattr(asset_hub, "migrate_legacy_layout_assets", fake_migrate)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "drive",
            "layout",
            "migrate",
            "--from",
            "compact_v2",
            "--to",
            "compact_v3",
            "--scope",
            "title",
            "--title",
            "mi-otra-yo",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert captured["from_layout"] == "compact_v2"
    assert captured["to_layout"] == "compact_v3"
    assert captured["scope"] == "title"
    assert captured["title_slug"] == "mi-otra-yo"
    assert "ASSETS_MATCHED=1" in output
    assert "ASSETS_TO_MOVE=1" in output
    assert "DRIVE_MUTATIONS=0" in output
    assert "layout_before=compact_v2 layout_after=compact_v3 changed=yes" in output


def test_layout_migrate_title_scope_requires_title(tmp_path: Path) -> None:
    env_file = write_env(tmp_path)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "drive",
            "layout",
            "migrate",
            "--from",
            "compact_v2",
            "--to",
            "compact_v3",
            "--scope",
            "title",
            "--quiet",
        ]
    )

    assert code == 2


def test_delete_empty_folders_requires_apply(tmp_path: Path) -> None:
    env_file = write_env(tmp_path)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "drive",
            "layout",
            "migrate",
            "--from",
            "legacy",
            "--to",
            "compact_v3",
            "--delete-empty-folders",
            "--quiet",
        ]
    )

    assert code == 2


def test_secrets_never_appear_in_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "super-secret-key")

    args = SimpleNamespace(quiet=False, json=False)
    code = asset_hub.fail(args, 2, RuntimeError("bad super-secret-key value"))

    captured = capsys.readouterr()
    assert code == 2
    assert "super-secret-key" not in captured.err
    assert "[REDACTED]" in captured.err


def test_advisory_lock_exit_code_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)

    def fake_run(_session, _request):
        return fake_batch_result(apply=False, lock_acquired=False)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", fake_run)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "batch", "--quiet"])

    assert code == 3


def test_second_apply_noop_exit_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path)

    def fake_run(_session, _request):
        return fake_batch_result(apply=True, files_applied=0, already_applied=1)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_drive_batch", fake_run)

    code = asset_hub.main(["--env-file", str(env_file), "drive", "batch", "--apply", "--quiet"])

    assert code == 0


def test_editorial_status_cli_outputs_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = write_env(tmp_path)

    def fake_status(_session, *, profile_version):
        return {
            "total": 4,
            "pending": 1,
            "searchable": 2,
            "quarantined": 1,
            "rejected": 0,
            "pipeline": {
                "profile_version": profile_version,
                "processed": 3,
                "failed": 0,
                "remaining": 1,
            },
        }

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "editorial_quality_status", fake_status)

    code = asset_hub.main(["--env-file", str(env_file), "editorial", "status"])

    output = capsys.readouterr().out
    assert code == 0
    assert "TOTAL=4" in output
    assert "PENDING=1" in output
    assert "REMAINING=1" in output


def test_editorial_quality_backfill_cli_passes_batch_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_backfill(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "dry_run": not kwargs["apply"],
                "profile_version": kwargs["profile_version"],
                "limit": kwargs["limit"],
                "batch_size": kwargs["batch_size"],
                "selected": 10,
                "processed": 10,
                "skipped": 0,
                "failed": 0,
                "remaining": 0,
            }
        )

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_editorial_quality_backfill", fake_backfill)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "editorial",
            "quality-backfill",
            "--limit",
            "10",
            "--batch-size",
            "5",
            "--apply",
            "--force",
            "--quiet",
        ]
    )

    assert code == 0
    assert captured["limit"] == 10
    assert captured["batch_size"] == 5
    assert captured["apply"] is True
    assert captured["force"] is True


def test_visual_status_cli_outputs_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = write_env(tmp_path)

    def fake_status(_session, *, profile_version):
        return {
            "total_candidates": 4,
            "pipeline": {
                "profile_version": profile_version,
                "processed": 3,
                "failed": 1,
                "remaining": 1,
            },
        }

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "visual_intelligence_status", fake_status)

    code = asset_hub.main(["--env-file", str(env_file), "visual", "status"])

    output = capsys.readouterr().out
    assert code == 0
    assert "TOTAL_CANDIDATES=4" in output
    assert "PROFILE_VERSION=visual-v1" in output
    assert "FAILED=1" in output


def test_visual_reprocess_cli_passes_asset_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    captured = {}

    def fake_reprocess(_session, asset_ids, **kwargs):
        captured["asset_ids"] = asset_ids
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "dry_run": not kwargs["apply"],
                "profile_version": kwargs["profile_version"],
                "limit": len(asset_ids),
                "batch_size": len(asset_ids),
                "selected": len(asset_ids),
                "processed": len(asset_ids),
                "skipped": 0,
                "failed": 0,
                "remaining": 0,
            }
        )

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "reprocess_visual_assets", fake_reprocess)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "visual",
            "reprocess",
            "--asset-id",
            "101",
            "--asset-id",
            "102",
            "--apply",
            "--quiet",
        ]
    )

    assert code == 0
    assert captured["asset_ids"] == [101, 102]
    assert captured["apply"] is True
    assert captured["profile_version"] == "visual-v1"


def test_editorial_apply_does_not_require_pilot_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "assert_pilot_database", lambda _session: pytest.fail("pilot assert should not run"))
    monkeypatch.setattr(
        asset_hub,
        "run_editorial_quality_backfill",
        lambda _session, **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "dry_run": not kwargs["apply"],
                "profile_version": kwargs["profile_version"],
                "limit": kwargs["limit"],
                "batch_size": kwargs["batch_size"],
                "selected": 0,
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "remaining": 0,
            }
        ),
    )

    code = asset_hub.main(
        ["--env-file", str(env_file), "editorial", "quality-backfill", "--apply", "--quiet"]
    )

    assert code == 0


def test_editorial_quality_worker_once_runs_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = write_env(tmp_path)
    calls = []

    def fake_backfill(_session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "dry_run": not kwargs["apply"],
                "profile_version": kwargs["profile_version"],
                "limit": kwargs["limit"],
                "batch_size": kwargs["batch_size"],
                "selected": 1,
                "processed": 1,
                "skipped": 0,
                "failed": 0,
                "remaining": 0,
            }
        )

    monkeypatch.setattr(asset_hub, "pilot_session_factory", lambda _url=None: sqlite_factory())
    monkeypatch.setattr(asset_hub, "run_editorial_quality_backfill", fake_backfill)

    code = asset_hub.main(
        [
            "--env-file",
            str(env_file),
            "editorial",
            "quality-worker",
            "--batch-size",
            "3",
            "--once",
            "--apply",
            "--quiet",
        ]
    )

    assert code == 0
    assert len(calls) == 1
    assert calls[0]["limit"] == 3
    assert calls[0]["batch_size"] == 3
    assert calls[0]["apply"] is True


def sqlite_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    return sessionmaker(bind=engine, expire_on_commit=False)


def fake_batch_result(
    apply: bool,
    lock_acquired: bool = True,
    files_applied: int = 0,
    already_applied: int = 0,
    nvidia_requests: int = 1,
    openai_requests: int = 0,
):
    counters = SimpleNamespace(
        files_discovered=0,
        files_new=0,
        files_skipped=0,
        files_analyzed=0 if apply else 1,
        files_applied=files_applied,
        files_ready=0,
        files_sent_to_review=0,
        files_failed=0,
        nvidia_requests=nvidia_requests,
        nvidia_retries=0,
        openai_requests=openai_requests,
        drive_mutations_attempted=files_applied if apply else 0,
        drive_mutations_succeeded=files_applied if apply else 0,
        duplicates=0,
        pending_analysis=0,
        already_applied=already_applied,
    )
    data = {
        "run_mode": "apply" if apply else "dry-run",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "concurrency": 1,
        "lock": "acquired" if lock_acquired else "blocked",
        "duration_seconds": 0,
        **counters.__dict__,
        "assets": [],
    }
    return SimpleNamespace(lock_acquired=lock_acquired, counters=counters, to_dict=lambda: data)


def patch_green_doctor(monkeypatch: pytest.MonkeyPatch, skip: set[str] | None = None) -> None:
    skip = skip or set()
    for name in (
        "check_database_access",
        "check_current_database",
        "check_alembic_head",
        "check_legacy_not_selected",
        "check_advisory_lock",
        "check_previews_writable",
        "check_rclone_remote",
        "check_oauth_renewable",
        "check_drive_access",
        "check_nvidia_key",
    ):
        if name not in skip:
            monkeypatch.setattr(asset_hub, name, lambda _args, _session, name=name: asset_hub.check(name, True))


def write_env(tmp_path: Path) -> Path:
    env_file = tmp_path / ".env.pilot"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=sqlite:///:memory:",
                "PILOT_DATABASE_URL=sqlite:///:memory:",
                "GOOGLE_DRIVE_ROOT_FOLDER_ID=root",
                "GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID=generic-inbox",
                "GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID=brand-inbox",
                "GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID=title-inbox",
                "RCLONE_REMOTE=fake",
                "NVIDIA_API_KEY=test-key",
            ]
        ),
        encoding="utf-8",
    )
    return env_file


def add_source(session: Session) -> Source:
    source = Source(source_id="managed-drive-pilot-test", provider="google_drive", label="Managed Drive Pilot")
    session.add(source)
    session.commit()
    return source


def add_asset(session: Session, source: Source, file_id: str, status: str, scope: str) -> Asset:
    brand = None
    if scope == "brand":
        brand = Brand(slug="grandiosa-mujer", name="Grandiosa Mujer")
        session.add(brand)
        session.flush()
    asset = Asset(
        asset_uid=f"uid-{file_id}",
        source=source,
        provider="google_drive",
        remote_path=f"90_revision/{scope}/video/lote-0001/{file_id}.mp4",
        source_path=f"90_revision/{scope}/video/lote-0001/{file_id}.mp4",
        drive_file_id=file_id,
        remote_file_id=file_id,
        filename=f"{file_id}.mp4",
        original_name=f"{file_id}.mp4",
        original_parent_id=f"{scope}-inbox",
        target_name=f"{file_id}.mp4",
        status=status,
        move_status="planned" if status in {"move_planned", "review_required"} else "moved",
        scope=scope,
        brand=brand,
        collection="evergreen" if scope == "brand" else None,
        type="video",
        mime_type="video/mp4",
        orientation="horizontal-16x9",
        primary_theme="personas",
        primary_topic="bienestar yoga",
        path_layout_version="legacy" if status == "review_required" else "compact_v3",
        plan_hash="old-hash",
    )
    session.add(asset)
    session.commit()
    return asset
