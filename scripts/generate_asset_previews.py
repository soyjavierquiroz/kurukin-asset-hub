#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Asset
from app.services.asset_preview import generate_asset_preview, local_preview_file


@dataclass
class Summary:
    processed: int = 0
    generated: int = 0
    skipped: int = 0
    missing_clips: int = 0
    errors: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate previews for video assets.")
    parser.add_argument("--missing", action="store_true", help="Only generate missing thumbnails.")
    parser.add_argument("--force", action="store_true", help="Regenerate existing previews.")
    parser.add_argument("--asset", type=int, help="Process a single asset ID.")
    parser.add_argument("--limit", type=int, help="Limit assets processed.")
    parser.add_argument("--dry-run", action="store_true", help="Show work without writing files or DB rows.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero.")
    if args.missing and args.force:
        parser.error("--missing and --force are mutually exclusive.")
    return args


def thumbnail_exists(asset: Asset) -> bool:
    path = local_preview_file(asset.thumbnail_path)
    return path is not None and path.is_file() and path.stat().st_size > 0


def selected_asset_ids(args: argparse.Namespace) -> list[int]:
    with SessionLocal() as session:
        query = select(Asset).where(Asset.type == "video").order_by(Asset.id.asc())
        if args.asset:
            query = query.where(Asset.id == args.asset)
        if args.limit:
            query = query.limit(args.limit)
        assets = session.scalars(query).all()
        if args.missing:
            assets = [asset for asset in assets if not thumbnail_exists(asset)]
        return [asset.id for asset in assets]


def main() -> int:
    args = parse_args()
    ids = selected_asset_ids(args)
    summary = Summary()
    print(f"selected={len(ids)}")
    for asset_id in ids:
        with SessionLocal() as session:
            asset = session.get(Asset, asset_id)
            if asset is None or asset.type != "video":
                summary.skipped += 1
                continue
            had_thumbnail = thumbnail_exists(asset)
            if args.dry_run:
                action = "would_generate"
                if had_thumbnail and not args.force:
                    action = "would_skip_existing"
                print(f"{action} asset={asset.id} remote_path={asset.remote_path}")
                summary.processed += 1
                summary.skipped += 1 if action == "would_skip_existing" else 0
                continue
            if had_thumbnail and not args.force and not args.missing:
                print(f"skip_existing asset={asset.id}")
                summary.processed += 1
                summary.skipped += 1
                continue
            try:
                result = generate_asset_preview(session, asset.id, force=args.force or not had_thumbnail)
                summary.processed += 1
                if result.preview_status == "ready" and thumbnail_exists(result):
                    summary.generated += 1
                    print(f"generated asset={result.id} thumbnail_path={result.thumbnail_path}")
                elif result.preview_status == "failed":
                    message = result.preview_error or "preview failed"
                    if "rclone" in message.lower() or "not found" in message.lower():
                        summary.missing_clips += 1
                    else:
                        summary.errors += 1
                    print(f"failed asset={result.id} error={message}")
                else:
                    summary.skipped += 1
                    print(f"skipped asset={result.id} status={result.preview_status}")
            except KeyboardInterrupt:
                session.rollback()
                raise
            except Exception as exc:
                session.rollback()
                summary.errors += 1
                print(f"error asset={asset_id} error={exc}")

    print(f"processed={summary.processed}")
    print(f"generated={summary.generated}")
    print(f"skipped={summary.skipped}")
    print(f"missing_clips={summary.missing_clips}")
    print(f"errors={summary.errors}")
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
