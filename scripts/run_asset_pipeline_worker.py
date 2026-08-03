#!/usr/bin/env python3
from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services.asset_pipeline import (
    AssetPipelineSummary,
    claim_pipeline_assets,
    pending_pipeline_asset_ids,
    process_asset_pipeline_batch,
    reset_stale_processing_assets,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("asset_pipeline_worker")


def process_worker_batch(limit: int) -> AssetPipelineSummary:
    with SessionLocal() as session:
        reset_stale_processing_assets(session)
        asset_ids = pending_pipeline_asset_ids(session, limit)
        claim_pipeline_assets(session, asset_ids)
        return process_asset_pipeline_batch(session, asset_ids)


def main() -> int:
    settings = get_settings()
    logger.info(
        "asset pipeline worker started batch_size=%s poll_seconds=%s preview_storage_dir=%s",
        settings.asset_pipeline_batch_size,
        settings.asset_pipeline_poll_seconds,
        settings.preview_storage_dir,
    )
    while True:
        summary = process_worker_batch(settings.asset_pipeline_batch_size)
        if summary.selected or summary.processed:
            logger.info(
                "asset pipeline batch selected=%s previews_ready=%s previews_failed=%s "
                "previews_skipped=%s ai_ready=%s ai_needs_review=%s ai_failed=%s ai_skipped=%s",
                summary.selected,
                summary.previews_ready,
                summary.previews_failed,
                summary.previews_skipped,
                summary.ai_ready,
                summary.ai_needs_review,
                summary.ai_failed,
                summary.ai_skipped,
            )
        time.sleep(settings.asset_pipeline_poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
