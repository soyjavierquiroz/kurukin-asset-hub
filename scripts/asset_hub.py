#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.services.managed_drive_pilot import (
    BatchLockUnavailable,
    BatchRequest,
    IngestRequest,
    PreflightRequest,
    ReviewApproval,
    acquire_batch_lock,
    default_batch_scopes,
    drive_client_from_environment,
    drive_auth_status,
    list_drive_status,
    list_review_required_assets,
    migrate_legacy_layout_assets,
    mutation_client_for_apply,
    preflight_drive_access,
    rename_reviewed_moved_asset,
    reconcile_rollback_required_assets,
    release_batch_lock,
    replan_reviewed_assets,
    run_drive_batch,
    approve_review_asset,
    validate_batch_source_selector,
)
from app.services.editorial_quality import (
    QUALITY_PROFILE_VERSION,
    editorial_quality_status,
    run_editorial_quality_backfill,
)
from app.services.visual_intelligence import (
    VISUAL_PROFILE_VERSION,
    reprocess_visual_assets,
    run_visual_intelligence_backfill,
    visual_intelligence_status,
)

EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_INVALID_CONFIG = 2
EXIT_LOCK_BUSY = 3
EXIT_NOT_APPLICABLE = 4
EXIT_MUTATION_FAILED = 5
PILOT_DATABASE = "kurukin_asset_hub_pilot"
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE_URL", "RCLONE_CONFIG")
REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=".env.pilot")
    common.add_argument("--database-url")
    common.add_argument("--json", action="store_true")
    common.add_argument("--verbose", action="store_true")
    common.add_argument("--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="asset-hub", parents=[common])
    subparsers = parser.add_subparsers(dest="resource", required=True)
    editorial = subparsers.add_parser("editorial", parents=[common])
    editorial_subparsers = editorial.add_subparsers(dest="action", required=True)
    editorial_status = editorial_subparsers.add_parser("status", parents=[common])
    editorial_status.add_argument("--profile-version", default=QUALITY_PROFILE_VERSION)
    quality = editorial_subparsers.add_parser("quality-backfill", parents=[common])
    quality.add_argument("--limit", type=int)
    quality.add_argument("--batch-size", type=int, default=20)
    quality.add_argument("--apply", action="store_true")
    quality.add_argument("--force", action="store_true")
    quality.add_argument("--profile-version", default=QUALITY_PROFILE_VERSION)
    worker = editorial_subparsers.add_parser("quality-worker", parents=[common])
    worker.add_argument("--batch-size", type=int, default=20)
    worker.add_argument("--interval-seconds", type=float, default=300.0)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--max-iterations", type=int)
    worker.add_argument("--apply", action="store_true")
    worker.add_argument("--force", action="store_true")
    worker.add_argument("--profile-version", default=QUALITY_PROFILE_VERSION)

    visual = subparsers.add_parser("visual", parents=[common])
    visual_subparsers = visual.add_subparsers(dest="action", required=True)
    visual_status = visual_subparsers.add_parser("status", parents=[common])
    visual_status.add_argument("--profile-version", default=VISUAL_PROFILE_VERSION)
    visual_backfill = visual_subparsers.add_parser("backfill", parents=[common])
    visual_backfill.add_argument("--limit", type=int)
    visual_backfill.add_argument("--batch-size", type=int, default=20)
    visual_backfill.add_argument("--apply", action="store_true")
    visual_backfill.add_argument("--force", action="store_true")
    visual_backfill.add_argument("--title-slug")
    visual_backfill.add_argument("--profile-version", default=VISUAL_PROFILE_VERSION)
    visual_reprocess = visual_subparsers.add_parser("reprocess", parents=[common])
    visual_reprocess.add_argument("--asset-id", type=int, action="append", required=True)
    visual_reprocess.add_argument("--apply", action="store_true")
    visual_reprocess.add_argument("--profile-version", default=VISUAL_PROFILE_VERSION)

    drive = subparsers.add_parser("drive", parents=[common])
    drive_subparsers = drive.add_subparsers(dest="action", required=True)

    batch = drive_subparsers.add_parser(
        "batch",
        parents=[common],
        description="Plan new Drive assets by default; apply only persisted plans with --apply.",
    )
    batch.add_argument("--apply", action="store_true")
    batch.add_argument("--max-total", type=int, default=50)
    batch.add_argument("--max-per-scope", type=int, default=25)
    batch.add_argument("--concurrency", type=int, default=1)
    batch.add_argument("--only-file-id", action="append", default=[])
    batch.add_argument("--scope", choices=["generic", "brand", "title"])
    batch.add_argument("--brand", dest="brand_slug")
    batch.add_argument("--title", dest="title_slug")

    replan_reviewed = drive_subparsers.add_parser(
        "replan-reviewed",
        parents=[common],
        description="Recalculate persisted plans for human-approved planned assets without moving Drive files.",
    )
    replan_reviewed.add_argument("--apply", action="store_true")
    replan_reviewed.add_argument("--only-file-id")
    replan_reviewed.add_argument("--limit", type=int, default=100)

    rename_reviewed = drive_subparsers.add_parser(
        "rename-reviewed",
        parents=[common],
        description="Rename one moved reviewed Drive asset in place after recalculating its human-approved filename.",
    )
    rename_reviewed.add_argument("--apply", action="store_true")
    rename_reviewed.add_argument("--only-file-id", required=True)

    status = drive_subparsers.add_parser("status", parents=[common])
    status.add_argument("--status", choices=["ready", "move_planned", "review_required", "failed"])
    status.add_argument("--scope", choices=["generic", "brand", "title"])
    status.add_argument("--limit", type=int, default=100)

    doctor = drive_subparsers.add_parser("doctor", parents=[common])
    doctor.add_argument("--check-ai", action="store_true")

    review = drive_subparsers.add_parser("review", parents=[common])
    review_subparsers = review.add_subparsers(dest="review_action", required=True)
    review_list = review_subparsers.add_parser("list", parents=[common])
    review_list.add_argument("--limit", type=int, default=100)
    approve = review_subparsers.add_parser("approve", parents=[common])
    approve.add_argument("--file-id", required=True)
    approve.add_argument("--primary-theme")
    approve.add_argument("--primary-topic")
    approve.add_argument("--tags", help="Comma-separated tags.")
    approve.add_argument("--reviewed-by")
    approve.add_argument("--yes", action="store_true", help="Confirm plan update without prompting.")
    reconcile = review_subparsers.add_parser(
        "reconcile",
        parents=[common],
        description="Repair review_required/rollback_required assets after transient Drive or OAuth failures.",
    )
    reconcile.add_argument("--apply", action="store_true")
    reconcile.add_argument("--limit", type=int, default=500)

    layout = drive_subparsers.add_parser("layout", parents=[common])
    layout_subparsers = layout.add_subparsers(dest="layout_action", required=True)
    migrate = layout_subparsers.add_parser("migrate", parents=[common])
    migrate.add_argument("--from", dest="from_layout", required=True, choices=["legacy", "compact_v2"])
    migrate.add_argument("--to", dest="to_layout", required=True, choices=["compact_v3"])
    migrate.add_argument("--scope", choices=["generic", "brand", "title"])
    migrate.add_argument("--brand", dest="brand_slug")
    migrate.add_argument("--title", dest="title_slug")
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--delete-empty-folders", action="store_true")

    ingest = drive_subparsers.add_parser("ingest", parents=[common], description="Legacy targeted ingest.")
    ingest.add_argument("--source-folder-id", required=True)
    ingest.add_argument("--destination-root-id", required=True)
    ingest.add_argument("--scope", required=True, choices=["generic", "brand", "title"])
    ingest.add_argument("--limit", type=int, default=10)
    ingest.add_argument("--file-id")
    ingest.add_argument("--brand", dest="brand_slug")
    ingest.add_argument("--collection", default="evergreen")
    ingest.add_argument("--title-type", choices=["movie", "series"])
    ingest.add_argument("--title")
    ingest.add_argument("--season", type=int)
    ingest.add_argument("--episode", type=int)
    ingest.add_argument("--replan", action="store_true")
    ingest.add_argument("--simulate-drive-mutation", action="store_true")
    mode = ingest.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")

    preflight = drive_subparsers.add_parser("preflight", parents=[common])
    preflight.add_argument("--source-folder-id", required=True)
    preflight.add_argument("--destination-root-id", required=True)
    preflight.add_argument("--file-id")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        load_runtime_config(args)
        if args.resource == "drive" and args.action == "doctor":
            return handle_doctor(args)
        if args.resource in {"drive", "editorial", "visual"}:
            session_factory = pilot_session_factory(args.database_url)
            with session_factory() as session:
                if args.resource in {"drive", "visual"} and mutating_command(args):
                    assert_pilot_database(session)
                if args.resource == "editorial":
                    return handle_editorial(args, session)
                if args.resource == "visual":
                    return handle_visual(args, session)
                return handle_drive(args, session)
    except BatchLockUnavailable as exc:
        return fail(args, EXIT_LOCK_BUSY, exc)
    except (ValueError, RuntimeError) as exc:
        return fail(args, EXIT_INVALID_CONFIG, exc)
    except SQLAlchemyError as exc:
        return fail(args, EXIT_OPERATIONAL_FAILURE, exc)
    except Exception as exc:
        return fail(args, EXIT_OPERATIONAL_FAILURE, exc)
    return EXIT_INVALID_CONFIG


def handle_editorial(args: argparse.Namespace, session: Session) -> int:
    if args.action == "status":
        data = editorial_quality_status(session, profile_version=args.profile_version)
        output(args, data, editorial_status_lines(data))
        return EXIT_SUCCESS
    if args.action == "quality-backfill":
        result = run_editorial_quality_backfill(
            session,
            limit=args.limit,
            batch_size=args.batch_size,
            apply=args.apply,
            force=args.force,
            profile_version=args.profile_version,
        )
        data = result.to_dict()
        output(args, data, editorial_backfill_lines(data))
        return EXIT_OPERATIONAL_FAILURE if data["failed"] else EXIT_SUCCESS
    if args.action == "quality-worker":
        return handle_editorial_quality_worker(args, session)
    return EXIT_INVALID_CONFIG


def handle_editorial_quality_worker(args: argparse.Namespace, session: Session) -> int:
    iterations = 0
    last_failed = 0
    while True:
        result = run_editorial_quality_backfill(
            session,
            limit=args.batch_size,
            batch_size=args.batch_size,
            apply=args.apply,
            force=args.force,
            profile_version=args.profile_version,
        )
        data = result.to_dict()
        output(args, data, editorial_backfill_lines(data))
        last_failed = data["failed"]
        iterations += 1
        if args.once or (args.max_iterations is not None and iterations >= args.max_iterations):
            break
        if data["remaining"] == 0 and not args.force:
            time.sleep(max(1.0, args.interval_seconds))
            continue
        time.sleep(max(0.0, args.interval_seconds))
    return EXIT_OPERATIONAL_FAILURE if last_failed else EXIT_SUCCESS


def handle_visual(args: argparse.Namespace, session: Session) -> int:
    if args.action == "status":
        data = visual_intelligence_status(session, profile_version=args.profile_version)
        output(args, data, visual_status_lines(data))
        return EXIT_SUCCESS
    if args.action == "backfill":
        result = run_visual_intelligence_backfill(
            session,
            limit=args.limit,
            batch_size=args.batch_size,
            apply=args.apply,
            force=args.force,
            title_slug=args.title_slug,
            profile_version=args.profile_version,
        )
        data = result.to_dict()
        output(args, data, visual_backfill_lines(data))
        return EXIT_OPERATIONAL_FAILURE if data["failed"] else EXIT_SUCCESS
    if args.action == "reprocess":
        result = reprocess_visual_assets(
            session,
            args.asset_id,
            apply=args.apply,
            profile_version=args.profile_version,
        )
        data = result.to_dict()
        output(args, data, visual_backfill_lines(data))
        return EXIT_OPERATIONAL_FAILURE if data["failed"] else EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def handle_drive(args: argparse.Namespace, session: Session) -> int:
    if args.action == "batch":
        request = BatchRequest(
            apply=args.apply,
            max_total=args.max_total,
            max_per_scope=args.max_per_scope,
            concurrency=args.concurrency,
            only_file_ids=tuple(args.only_file_id),
            scope=args.scope,
            brand_slug=args.brand_slug,
            title_slug=args.title_slug,
        )
        validate_batch_source_selector(request)
        assert_pilot_database(session)
        result = run_drive_batch(
            session,
            request,
        )
        output(args, result.to_dict(), batch_lines(result.to_dict()))
        if not result.lock_acquired:
            return EXIT_LOCK_BUSY
        if result.counters.files_failed:
            return EXIT_MUTATION_FAILED if args.apply else EXIT_OPERATIONAL_FAILURE
        return EXIT_SUCCESS
    if args.action == "status":
        result = list_drive_status(session, status=args.status, scope=args.scope, limit=args.limit)
        output(args, result.to_dict(), status_lines(result.to_dict()))
        return EXIT_SUCCESS
    if args.action == "replan-reviewed":
        results = replan_reviewed_assets(
            session,
            apply=args.apply,
            only_file_id=args.only_file_id,
            limit=args.limit,
        )
        data = {
            "dry_run": not args.apply,
            "assets_checked": len(results),
            "assets_changed": sum(1 for result in results if result.changed),
            "assets": [result.to_dict() for result in results],
        }
        output(args, data, replan_lines(data))
        return EXIT_SUCCESS
    if args.action == "rename-reviewed":
        drive_client = mutation_client_for_repair() if args.apply else None
        result = rename_reviewed_moved_asset(
            session,
            drive_client,
            file_id=args.only_file_id,
            apply=args.apply,
        )
        data = result.to_dict()
        output(args, data, rename_lines(data))
        return EXIT_SUCCESS
    if args.action == "review":
        return handle_review(args, session)
    if args.action == "layout":
        return handle_layout(args, session)
    if args.action == "ingest":
        from app.services.managed_drive_pilot import ingest_drive_assets

        if args.apply:
            assert_pilot_database(session)
        plans = ingest_drive_assets(
            session,
            IngestRequest(
                source_folder_id=args.source_folder_id,
                destination_root_id=args.destination_root_id,
                scope=args.scope,
                limit=args.limit,
                apply=args.apply,
                dry_run=not args.apply,
                file_id=args.file_id,
                brand_slug=args.brand_slug,
                collection=args.collection,
                title_type=args.title_type,
                title=args.title,
                season=args.season,
                episode=args.episode,
                replan=args.replan,
                simulate_drive_mutation=args.simulate_drive_mutation,
            ),
        )
        output(args, [plan.to_dict() for plan in plans], [json.dumps([plan.to_dict() for plan in plans])])
        return EXIT_SUCCESS
    if args.action == "preflight":
        result = preflight_drive_access(PreflightRequest(args.source_folder_id, args.destination_root_id, args.file_id))
        output(args, {"lines": result.to_lines()}, result.to_lines())
        return EXIT_SUCCESS if result.ready_for_dry_run else EXIT_OPERATIONAL_FAILURE
    return EXIT_INVALID_CONFIG


def handle_review(args: argparse.Namespace, session: Session) -> int:
    if args.review_action == "list":
        result = list_review_required_assets(session, limit=args.limit)
        output(args, result.to_dict(), status_lines(result.to_dict()))
        return EXIT_SUCCESS if result.rows else EXIT_NOT_APPLICABLE
    if args.review_action == "approve":
        if not args.yes and not sys.stdin.isatty():
            raise ValueError("review approve requires --yes in non-interactive mode")
        if not args.yes:
            answer = input(f"Update plan for {args.file_id} without moving the file? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                return EXIT_NOT_APPLICABLE
        row = approve_review_asset(
            session,
            ReviewApproval(
                file_id=args.file_id,
                primary_theme=args.primary_theme,
                primary_topic=args.primary_topic,
                tags=tuple(split_tags(args.tags)),
                reviewed_by=args.reviewed_by,
            ),
        )
        output(args, row.to_dict(), row_lines([row.to_dict()]))
        return EXIT_SUCCESS
    if args.review_action == "reconcile":
        drive_client = drive_client_from_environment()
        results = reconcile_rollback_required_assets(
            session,
            drive_client,
            apply=args.apply,
            limit=args.limit,
        )
        data = reconcile_payload(results, apply=args.apply)
        output(args, data, reconcile_lines(data))
        return EXIT_OPERATIONAL_FAILURE if data["summary"]["FILES_FAILED"] else EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def handle_layout(args: argparse.Namespace, session: Session) -> int:
    if args.layout_action != "migrate":
        return EXIT_INVALID_CONFIG
    validate_layout_migrate_args(args)
    if args.delete_empty_folders and not args.apply:
        raise ValueError("--delete-empty-folders requires --apply")
    assert_pilot_database(session) if args.apply else None
    settings = get_settings()
    if not settings.google_drive_root_folder_id:
        raise ValueError("GOOGLE_DRIVE_ROOT_FOLDER_ID is required")
    read_client = drive_client_from_environment()
    drive_client = mutation_client_for_apply(read_client) if args.apply else read_client
    result = migrate_legacy_layout_assets(
        session,
        drive_client,
        settings.google_drive_root_folder_id,
        from_layout=args.from_layout,
        to_layout=args.to_layout,
        scope=args.scope,
        brand_slug=args.brand_slug,
        title_slug=args.title_slug,
        simulate=not args.apply,
        cleanup_empty_folders=args.delete_empty_folders,
    )
    output(args, result.to_dict(), layout_lines(result.to_dict()))
    return EXIT_SUCCESS


def validate_layout_migrate_args(args: argparse.Namespace) -> None:
    if args.to_layout != "compact_v3":
        raise ValueError(f"unsupported target layout: {args.to_layout}")
    if args.from_layout not in {"legacy", "compact_v2"}:
        raise ValueError(f"unsupported source layout: {args.from_layout}")
    if args.scope == "title" and not args.title_slug:
        raise ValueError("--scope title requires --title")
    if args.scope == "brand" and not args.brand_slug:
        raise ValueError("--scope brand requires --brand")
    if args.from_layout == "compact_v2" and not args.scope:
        raise ValueError("--from compact_v2 requires --scope")


def handle_doctor(args: argparse.Namespace) -> int:
    session_factory = pilot_session_factory(args.database_url)
    checks: list[dict[str, Any]] = []
    ok = True
    with session_factory() as session:
        for check in (
            check_database_access,
            check_current_database,
            check_alembic_head,
            check_legacy_not_selected,
            check_advisory_lock,
            check_previews_writable,
        ):
            item = check(args, session)
            checks.append(item)
            ok = ok and item["ok"]
    for check in (check_rclone_remote, check_oauth_renewable, check_drive_access, check_nvidia_key):
        item = check(args, None)
        checks.append(item)
        ok = ok and item["ok"]
    if args.check_ai:
        item = check_nvidia_model(args, None)
        checks.append(item)
        ok = ok and item["ok"]
    data = {"status": "OK" if ok else "FAIL", "checks": checks}
    lines = [f"{item['name']}={'OK' if item['ok'] else 'FAIL'}" for item in checks]
    lines.append(f"DOCTOR_STATUS={'OK' if ok else 'FAIL'}")
    output(args, data, lines)
    return EXIT_SUCCESS if ok else EXIT_OPERATIONAL_FAILURE


def load_runtime_config(args: argparse.Namespace) -> None:
    load_pilot_env(Path(args.env_file))
    if args.database_url:
        os.environ["PILOT_DATABASE_URL"] = args.database_url
    get_settings.cache_clear()


def load_pilot_env(path: Path = Path(".env.pilot")) -> None:
    if not path.exists():
        raise RuntimeError(f"env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("\"'")


def pilot_session_factory(database_url: str | None = None) -> sessionmaker:
    settings = get_settings()
    url = database_url or settings.pilot_database_url or settings.database_url
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def assert_pilot_database(session: Session) -> None:
    current = current_database_name(session)
    if current != PILOT_DATABASE:
        raise RuntimeError(f"Refusing mutation: current_database() is {current!r}, expected {PILOT_DATABASE!r}")


def current_database_name(session: Session) -> str:
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        return str(session.execute(text("SELECT current_database()")).scalar() or "")
    if dialect == "sqlite":
        return PILOT_DATABASE
    return ""


def mutating_command(args: argparse.Namespace) -> bool:
    if getattr(args, "resource", None) == "editorial":
        return getattr(args, "action", None) in {"quality-backfill", "quality-worker"} and bool(
            getattr(args, "apply", False)
        )
    if getattr(args, "resource", None) == "visual":
        return getattr(args, "action", None) in {"backfill", "reprocess"} and bool(
            getattr(args, "apply", False)
        )
    if args.action == "batch":
        return bool(args.apply)
    if args.action in {"replan-reviewed", "rename-reviewed"}:
        return bool(getattr(args, "apply", False))
    if args.action == "review":
        if getattr(args, "review_action", "") == "approve":
            return True
        if getattr(args, "review_action", "") == "reconcile":
            return bool(getattr(args, "apply", False))
        return False
    if args.action == "layout":
        return bool(getattr(args, "apply", False))
    if args.action == "ingest":
        return bool(getattr(args, "apply", False))
    return False


def check_database_access(_args: argparse.Namespace, session: Session) -> dict[str, Any]:
    try:
        session.execute(text("SELECT 1")).scalar()
        return check("postgresql_pilot_accessible", True)
    except OperationalError as exc:
        return check("postgresql_pilot_accessible", False, exc)


def check_current_database(_args: argparse.Namespace, session: Session) -> dict[str, Any]:
    current = current_database_name(session)
    return check("current_database", current == PILOT_DATABASE, f"current_database={current}")


def check_alembic_head(_args: argparse.Namespace, session: Session) -> dict[str, Any]:
    try:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(config)
        expected = set(script.get_heads())
        existing = {
            str(row[0])
            for row in session.execute(text("SELECT version_num FROM alembic_version")).all()
        }
        return check("alembic_head", existing == expected, f"db={sorted(existing)} head={sorted(expected)}")
    except Exception as exc:
        return check("alembic_head", False, exc)


def check_legacy_not_selected(_args: argparse.Namespace, session: Session) -> dict[str, Any]:
    current = current_database_name(session)
    return check("legacy_database_not_selected", current == PILOT_DATABASE, f"current_database={current}")


def check_advisory_lock(_args: argparse.Namespace, session: Session) -> dict[str, Any]:
    locked = acquire_batch_lock(session)
    if locked:
        release_batch_lock(session)
    return check("advisory_lock_available", locked)


def check_previews_writable(_args: argparse.Namespace, _session: Session) -> dict[str, Any]:
    try:
        root = Path(get_settings().pilot_preview_root)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".doctor-", dir=root, delete=True) as file:
            file.write(b"ok")
        return check("previews_writable", True)
    except Exception as exc:
        return check("previews_writable", False, exc)


def check_rclone_remote(_args: argparse.Namespace, _session: Session | None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.rclone_remote:
        return check("rclone_remote_accessible", False, "RCLONE_REMOTE missing")
    try:
        subprocess.run(
            ["rclone", "lsjson", f"{settings.rclone_remote.rstrip(':')}:", "--max-depth", "1"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return check("rclone_remote_accessible", True)
    except Exception as exc:
        return check("rclone_remote_accessible", False, exc)


def check_oauth_renewable(_args: argparse.Namespace, _session: Session | None) -> dict[str, Any]:
    try:
        drive = drive_client_from_environment()
        auth = drive_auth_status(get_settings(), drive)
        return check(
            "oauth_renewable",
            auth["auth_refreshable"] and auth["oauth_scope_sufficient"],
            f"custom_oauth_client={auth['custom_oauth_client']}",
        )
    except Exception as exc:
        return check("oauth_renewable", False, exc)


def check_drive_access(_args: argparse.Namespace, _session: Session | None) -> dict[str, Any]:
    try:
        settings = get_settings()
        scopes = default_batch_scopes()
        drive = drive_client_from_environment()
        ok = bool(settings.google_drive_root_folder_id and drive.folder_exists(settings.google_drive_root_folder_id))
        for scope in scopes:
            ok = ok and drive.folder_exists(scope.source_folder_id)
        return check("drive_api_and_inboxes_accessible", ok)
    except Exception as exc:
        return check("drive_api_and_inboxes_accessible", False, exc)


def check_nvidia_key(_args: argparse.Namespace, _session: Session | None) -> dict[str, Any]:
    return check("nvidia_api_key_present", bool(get_settings().nvidia_api_key))


def check_nvidia_model(_args: argparse.Namespace, _session: Session | None) -> dict[str, Any]:
    try:
        from app.services.ai_providers.nvidia_provider import call_nvidia_vision

        call_nvidia_vision("Responde con JSON minimo de prueba.", [], model=get_settings().nvidia_model)
        return check("nvidia_model_responds", True)
    except Exception as exc:
        return check("nvidia_model_responds", False, exc)


def check(name: str, ok: bool, detail: object | None = None) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": sanitize_output(str(detail)) if detail else None}


def output(args: argparse.Namespace, data: Any, lines: list[str]) -> None:
    if args.quiet:
        return
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))


def fail(args: argparse.Namespace, code: int, exc: Exception) -> int:
    if not getattr(args, "quiet", False):
        message = sanitize_output(str(exc))
        if getattr(args, "json", False):
            print(json.dumps({"error": message, "exit_code": code}), file=sys.stderr)
        else:
            print(f"ERROR: {message}", file=sys.stderr)
    return code


def batch_lines(data: dict[str, Any]) -> list[str]:
    return [
        f"RUN_MODE={data['run_mode']}",
        f"LOCK={data['lock']}",
        f"FILES_DISCOVERED={data['files_discovered']}",
        f"PLANS_SELECTED={data.get('plans_selected', 0)}",
        f"FILES_NEW={data['files_new']}",
        f"FILES_ANALYZED={data['files_analyzed']}",
        f"FILES_APPLIED={data['files_applied']}",
        f"PENDING_ANALYSIS={data['pending_analysis']}",
        f"NVIDIA_REQUESTS={data['nvidia_requests']}",
        f"OPENAI_REQUESTS={data['openai_requests']}",
        f"DRIVE_MUTATIONS_ATTEMPTED={data['drive_mutations_attempted']}",
        f"FILES_FAILED={data['files_failed']}",
    ]


def status_lines(data: dict[str, Any]) -> list[str]:
    lines = [f"{key.upper()}={value}" for key, value in data["summary"].items()]
    lines.extend(row_lines(data["assets"]))
    return lines


def editorial_status_lines(data: dict[str, Any]) -> list[str]:
    pipeline = data["pipeline"]
    return [
        f"TOTAL={data['total']}",
        f"PENDING={data['pending']}",
        f"SEARCHABLE={data['searchable']}",
        f"QUARANTINED={data['quarantined']}",
        f"REJECTED={data['rejected']}",
        f"PROFILE_VERSION={pipeline['profile_version']}",
        f"PROCESSED={pipeline['processed']}",
        f"FAILED={pipeline['failed']}",
        f"REMAINING={pipeline['remaining']}",
    ]


def editorial_backfill_lines(data: dict[str, Any]) -> list[str]:
    return [
        f"DRY_RUN={'YES' if data['dry_run'] else 'NO'}",
        f"PROFILE_VERSION={data['profile_version']}",
        f"LIMIT={data['limit'] if data['limit'] is not None else ''}",
        f"BATCH_SIZE={data['batch_size']}",
        f"SELECTED={data['selected']}",
        f"PROCESSED={data['processed']}",
        f"SKIPPED={data['skipped']}",
        f"FAILED={data['failed']}",
        f"REMAINING={data['remaining']}",
    ]


def visual_status_lines(data: dict[str, Any]) -> list[str]:
    pipeline = data["pipeline"]
    return [
        f"TOTAL_CANDIDATES={data['total_candidates']}",
        f"PROFILE_VERSION={pipeline['profile_version']}",
        f"PROCESSED={pipeline['processed']}",
        f"FAILED={pipeline['failed']}",
        f"REMAINING={pipeline['remaining']}",
    ]


def visual_backfill_lines(data: dict[str, Any]) -> list[str]:
    lines = [
        f"DRY_RUN={'YES' if data['dry_run'] else 'NO'}",
        f"PROFILE_VERSION={data['profile_version']}",
        f"LIMIT={data['limit'] if data['limit'] is not None else ''}",
        f"BATCH_SIZE={data['batch_size']}",
        f"SELECTED={data['selected']}",
        f"PROCESSED={data['processed']}",
        f"SKIPPED={data['skipped']}",
        f"FAILED={data['failed']}",
        f"REMAINING={data['remaining']}",
    ]
    if data.get("title_slug") is not None:
        lines.insert(2, f"TITLE_SLUG={data['title_slug']}")
    return lines


def row_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = []
    for row in rows:
        lines.append(
            "\t".join(
                str(row.get(key) or "")
                for key in (
                    "drive_file_id",
                    "scope",
                    "original_name",
                    "current_name",
                    "status",
                    "move_status",
                    "requires_review",
                    "review_reason",
                    "provider",
                    "model",
                    "target_path",
                    "path_layout_version",
                    "updated_at",
                )
            )
        )
    return lines


def layout_lines(data: dict[str, Any]) -> list[str]:
    assets_matched = len(data["assets_detected"])
    assets_to_move = sum(
        1 for plan in data.get("plans", []) if plan.get("current_path") != plan.get("compact_target_path")
    )
    lines = [
        f"SIMULATED={'YES' if data['simulated'] else 'NO'}",
        f"ASSETS_DETECTED={len(data['assets_detected'])}",
        f"ASSETS_MATCHED={assets_matched}",
        f"ASSETS_TO_MOVE={assets_to_move}",
        f"ASSETS_SKIPPED={assets_matched - assets_to_move}",
        f"ASSETS_MIGRATED={data['assets_migrated']}",
        f"FOLDERS_CREATED={data['folders_created']}",
        f"FOLDERS_DELETED={data['folders_deleted']}",
        "FILES_FAILED=0",
        f"DRIVE_MUTATIONS={data['drive_mutations']}",
        f"DUPLICATES={data['duplicates']}",
    ]
    for plan in data.get("plans", []):
        changed = "yes" if plan.get("current_path") != plan.get("compact_target_path") else "no"
        updates = plan.get("database_updates", {})
        lines.append(
            " ".join(
                (
                    f"drive_file_id={plan.get('drive_file_id') or ''}",
                    f"path_before={plan.get('current_path') or ''}",
                    f"path_after={plan.get('compact_target_path') or ''}",
                    f"layout_before={updates.get('path_layout_version_before') or ''}",
                    f"layout_after={updates.get('path_layout_version') or ''}",
                    f"changed={changed}",
                )
            )
        )
    return lines


def replan_lines(data: dict[str, Any]) -> list[str]:
    lines = [
        f"DRY_RUN={'YES' if data['dry_run'] else 'NO'}",
        f"ASSETS_CHECKED={data['assets_checked']}",
        f"ASSETS_CHANGED={data['assets_changed']}",
    ]
    for row in data["assets"]:
        lines.append(
            "\t".join(
                str(row.get(key) or "")
                for key in (
                    "file_id",
                    "changed",
                    "skipped_reason",
                    "target_name_before",
                    "target_name_after",
                    "target_path_before",
                    "target_path_after",
                    "plan_hash_before",
                    "plan_hash_after",
                )
            )
        )
    return lines


def rename_lines(data: dict[str, Any]) -> list[str]:
    return [
        f"DRY_RUN={'YES' if data['dry_run'] else 'NO'}",
        f"FILE_ID={data['file_id']}",
        f"CHANGED={'YES' if data['changed'] else 'NO'}",
        f"SKIPPED_REASON={data['skipped_reason'] or ''}",
        f"PARENT_BEFORE={data['parent_before'] or ''}",
        f"PARENT_AFTER={data['parent_after'] or ''}",
        f"TARGET_NAME_BEFORE={data['target_name_before'] or ''}",
        f"TARGET_NAME_AFTER={data['target_name_after'] or ''}",
        f"TARGET_PATH_BEFORE={data['target_path_before'] or ''}",
        f"TARGET_PATH_AFTER={data['target_path_after'] or ''}",
    ]


def reconcile_payload(results: list[Any], *, apply: bool) -> dict[str, Any]:
    rows = [result.to_dict() for result in results]
    db_mutations = sum(1 for row in rows if row["status_before"] != row["status_after"] or row["move_status_before"] != row["move_status_after"])
    summary = {
        "ROLLBACK_REQUIRED_MATCHED": len(rows),
        "ALREADY_AT_TARGET": sum(1 for row in rows if row["reconciliation"] == "already_at_target"),
        "STILL_AT_SOURCE": sum(1 for row in rows if row["reconciliation"] == "still_at_source"),
        "MANUAL_REQUIRED": sum(1 for row in rows if row["reconciliation"] == "manual_required"),
        "DB_MUTATIONS": db_mutations if apply else 0,
        "DRIVE_MUTATIONS": 0,
        "FILES_FAILED": sum(1 for row in rows if str(row.get("reason") or "").startswith("drive_metadata_error")),
    }
    return {"dry_run": not apply, "summary": summary, "assets": rows}


def reconcile_lines(data: dict[str, Any]) -> list[str]:
    summary = data["summary"]
    lines = [f"DRY_RUN={'YES' if data['dry_run'] else 'NO'}"]
    lines.extend(f"{key}={value}" for key, value in summary.items())
    for row in data["assets"]:
        lines.append(
            "\t".join(
                str(row.get(key) or "")
                for key in (
                    "drive_file_id",
                    "current_name",
                    "current_parent_id",
                    "expected_source_parent_id",
                    "expected_target_path",
                    "reconciliation",
                    "action",
                    "reason",
                )
            )
        )
    return lines


def mutation_client_for_repair():
    return mutation_client_for_apply(drive_client_from_environment())


def split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def sanitize_output(value: str) -> str:
    sanitized = value
    for key, raw in os.environ.items():
        if not raw or len(raw) < 4 or not any(hint in key.upper() for hint in SECRET_HINTS):
            continue
        sanitized = sanitized.replace(raw, "[REDACTED]")
    return sanitized


if __name__ == "__main__":
    raise SystemExit(main())
