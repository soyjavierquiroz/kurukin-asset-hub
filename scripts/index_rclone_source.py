#!/usr/bin/env python
import argparse
import sys

from app.db import SessionLocal
from app.services.asset_indexer import AssetIndexContext, AssetIndexer
from app.services.rclone_service import RcloneError, RcloneService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index asset metadata from an rclone remote without downloading files.",
    )
    parser.add_argument("--source-id", required=True, help="Stable Source.source_id value.")
    parser.add_argument("--remote", required=True, help="rclone remote name, without trailing colon.")
    parser.add_argument("--root", required=True, help="Root folder inside the rclone remote.")
    parser.add_argument("--brand", help="Optional brand slug to assign.")
    parser.add_argument("--product", help="Optional product slug inside --brand to assign.")
    parser.add_argument("--default-niche", help="Optional niche slug to attach to indexed assets.")
    parser.add_argument(
        "--provider",
        default="google_drive",
        help="Provider stored in Source and Asset records. Defaults to google_drive.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    context = AssetIndexContext(
        source_id=args.source_id,
        remote=args.remote,
        root=args.root,
        brand_slug=args.brand,
        product_slug=args.product,
        default_niche_slug=args.default_niche,
        provider=args.provider,
    )

    try:
        print(f"Listing rclone remote '{args.remote}' under root '{args.root}'...")
        entries = RcloneService().list_json(args.remote, args.root)
        with SessionLocal() as session:
            try:
                summary = AssetIndexer(session).index_entries(entries, context)
                session.commit()
            except Exception:
                session.rollback()
                raise
    except (RcloneError, ValueError) as exc:
        print(f"Indexing failed: {exc}", file=sys.stderr)
        return 1

    print("Indexing complete")
    print(f"total listed: {summary.total_listed}")
    print(f"total allowed: {summary.total_allowed}")
    print(f"created: {summary.created}")
    print(f"updated: {summary.updated}")
    print(f"skipped: {summary.skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
