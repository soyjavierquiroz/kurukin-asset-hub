#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Asset
from app.services.ai_asset_enrichment import enrich_asset_with_ai


@dataclass
class Summary:
    total: int = 0
    processed: int = 0
    ready: int = 0
    needs_review: int = 0
    skipped: int = 0
    failed: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AI visual enrichment for catalog assets.")
    parser.add_argument("--asset-id", action="append", type=int, default=[])
    parser.add_argument("--pending", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.asset_id and not args.pending:
        parser.error("Use --asset-id at least once or --pending.")
    if args.limit < 1:
        parser.error("--limit must be greater than zero.")
    return args


def pending_asset_ids(limit: int) -> list[int]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Asset.id)
                .where(Asset.ai_enrichment_status == "pending")
                .order_by(Asset.created_at.asc(), Asset.id.asc())
                .limit(limit)
            )
        )


def main() -> int:
    args = parse_args()
    asset_ids = list(dict.fromkeys(args.asset_id))
    if args.pending:
        asset_ids.extend(
            asset_id for asset_id in pending_asset_ids(args.limit) if asset_id not in asset_ids
        )

    summary = Summary(total=len(asset_ids))
    for asset_id in asset_ids:
        with SessionLocal() as session:
            try:
                asset = enrich_asset_with_ai(
                    session,
                    asset_id,
                    force=args.force,
                    dry_run=args.dry_run,
                )
                summary.processed += 1
                if asset.ai_enrichment_status == "ready":
                    summary.ready += 1
                elif asset.ai_enrichment_status == "needs_review":
                    summary.needs_review += 1
                elif asset.ai_enrichment_status == "skipped":
                    summary.skipped += 1
                elif asset.ai_enrichment_status == "failed":
                    summary.failed += 1
            except KeyboardInterrupt:
                session.rollback()
                raise
            except Exception:
                session.rollback()
                summary.failed += 1
                continue

    print(f"total={summary.total}")
    print(f"processed={summary.processed}")
    print(f"ready={summary.ready}")
    print(f"needs_review={summary.needs_review}")
    print(f"skipped={summary.skipped}")
    print(f"failed={summary.failed}")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted")
        raise SystemExit(130)
