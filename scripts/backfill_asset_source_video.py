from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Asset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill assets.source_video when the parent is known.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary: dict[str, Any] = {
        "candidates": 0,
        "updated": 0,
        "left_null": 0,
        "dry_run": args.dry_run,
    }
    with SessionLocal() as session:
        query = (
            select(Asset)
            .where(
                Asset.source_video.is_(None),
                Asset.parent_asset_id.is_not(None),
                Asset.is_derivative.is_(True),
            )
            .order_by(Asset.id.asc())
        )
        if args.limit:
            query = query.limit(args.limit)
        assets = session.scalars(query).all()
        summary["candidates"] = len(assets)
        for asset in assets:
            parent = session.get(Asset, asset.parent_asset_id)
            if parent is None or not parent.remote_path:
                summary["left_null"] += 1
                continue
            summary["updated"] += 1
            if not args.dry_run:
                asset.source_video = parent.remote_path
        if not args.dry_run:
            session.commit()

    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
