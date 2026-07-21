from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Source
from app.services.source_sync import scan_source


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan or apply sync for an rclone-backed source.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--source-id",
        help="Public source_id, for example drive_grandiosa_mujer_veyra",
    )
    target.add_argument("--source-db-id", type=int, help="Database source id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Scan and save a dry-run report")
    mode.add_argument("--apply", action="store_true", help="Scan and apply changes")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_ref: int | str = args.source_db_id if args.source_db_id is not None else args.source_id
    with SessionLocal() as session:
        if args.source_id:
            source = session.scalar(select(Source).where(Source.source_id == args.source_id))
            if source is None:
                raise SystemExit(f"Source not found: {args.source_id}")
            source_ref = source.id
        run = scan_source(session, source_ref, apply=args.apply, created_by="cli")
        summary = run.summary_json or {}
        keys = [
            "total_remote",
            "new",
            "existing",
            "moved",
            "changed",
            "missing",
            "unsupported",
            "errors",
        ]
        for key in keys:
            print(f"{key}={summary.get(key, 0)}")
        print(f"run_uid={run.run_uid}")
        print(f"status={run.status}")
        if run.error:
            print(f"error={run.error}")


if __name__ == "__main__":
    main()
