from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Asset, RawVideo
from app.services.raw_video_ingestion import DEFAULT_RAW_VIDEO_PROVIDER


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill raw_videos from assets.source_video.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary: dict[str, Any] = {
        "source_videos": 0,
        "raw_videos_created": 0,
        "raw_videos_existing": 0,
        "assets_linked": 0,
        "dry_run": args.dry_run,
    }
    now = datetime.now(UTC)
    with SessionLocal() as session:
        rows = session.execute(
            select(Asset.source_video, func.count(Asset.id))
            .where(Asset.source_video.is_not(None))
            .group_by(Asset.source_video)
            .order_by(Asset.source_video.asc())
        ).all()
        summary["source_videos"] = len(rows)
        for source_video, segment_count in rows:
            raw_video = session.scalar(select(RawVideo).where(RawVideo.remote_path == source_video))
            if raw_video is None:
                raw_video = RawVideo(
                    remote_path=source_video,
                    provider=DEFAULT_RAW_VIDEO_PROVIDER,
                    status="DONE",
                    segments_generated=int(segment_count),
                    processed_at=now,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
                summary["raw_videos_created"] += 1
                if not args.dry_run:
                    session.add(raw_video)
                    session.flush()
            else:
                summary["raw_videos_existing"] += 1
                if not args.dry_run:
                    raw_video.status = "DONE"
                    raw_video.segments_generated = int(segment_count)
                    raw_video.processed_at = raw_video.processed_at or now
                    raw_video.last_seen_at = raw_video.last_seen_at or now
                    raw_video.updated_at = now

            if args.dry_run:
                continue

            assets = session.scalars(
                select(Asset).where(Asset.source_video == source_video, Asset.raw_video_id.is_(None))
            ).all()
            for asset in assets:
                asset.raw_video_id = raw_video.id
            summary["assets_linked"] += len(assets)

        if not args.dry_run:
            session.commit()

    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
