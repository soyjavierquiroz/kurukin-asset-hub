from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from app.db import SessionLocal
from app.services.long_video_segmentation import (
    LongVideoSegmentationError,
    run_to_summary,
    segment_long_video_asset,
    segment_long_videos_for_source,
)

DEFAULT_MISSING_SOURCE_ID = "gdrive_people_raw_long"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Segment long videos into derived b-roll assets.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--asset-id", type=int)
    target.add_argument("--source-id")
    target.add_argument("--missing", action="store_true", help="Process missing videos from gdrive_people_raw_long.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--derived-remote")
    parser.add_argument("--derived-root")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument(
        "--delete-original-after-approval",
        default="false",
        help="Reserved for explicit delete workflows; default false.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary: dict[str, Any] = {
        "videos_found": 0,
        "videos_processed": 0,
        "videos_skipped": 0,
        "segments_generated": 0,
        "errors": 0,
        "partial": 0,
        "runs": [],
    }
    try:
        with SessionLocal() as session:
            if args.asset_id:
                runs = [
                    segment_long_video_asset(
                        session,
                        args.asset_id,
                        force=args.force,
                        created_by="cli",
                        derived_remote=args.derived_remote,
                        derived_root=args.derived_root,
                        skip_ai=args.skip_ai,
                    )
                ]
                summary["videos_found"] = 1
            else:
                runs = segment_long_videos_for_source(
                    session,
                    DEFAULT_MISSING_SOURCE_ID if args.missing else args.source_id,
                    limit=args.limit,
                    force=args.force,
                    created_by="cli",
                    derived_remote=args.derived_remote,
                    derived_root=args.derived_root,
                    skip_ai=args.skip_ai,
                )
                summary["videos_found"] = len(runs)

            for run in runs:
                run_summary = run_to_summary(run)
                report = run_summary["report"] if isinstance(run_summary["report"], dict) else {}
                summary["runs"].append(run_summary)
                summary["segments_generated"] += int(report.get("children_created") or 0)
                if report.get("skipped"):
                    summary["videos_skipped"] += 1
                elif run.status == "ready":
                    summary["videos_processed"] += 1
                elif run.status == "partial":
                    summary["videos_processed"] += 1
                    summary["partial"] += 1
                elif run.status == "failed":
                    summary["errors"] += 1
    except LongVideoSegmentationError as exc:
        summary["errors"] += 1
        summary["error"] = str(exc)[:500]
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        return 1

    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
