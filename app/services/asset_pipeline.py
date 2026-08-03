from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Asset
from app.services.ai_asset_enrichment import enrich_asset_with_ai
from app.services.asset_preview import generate_asset_preview, local_preview_file


PROCESSABLE_TYPES = ("image", "video")
STALE_PROCESSING_AFTER = timedelta(minutes=15)


@dataclass
class AssetPipelineSummary:
    selected: int = 0
    previews_ready: int = 0
    previews_failed: int = 0
    previews_skipped: int = 0
    ai_ready: int = 0
    ai_needs_review: int = 0
    ai_failed: int = 0
    ai_skipped: int = 0

    @property
    def processed(self) -> int:
        return (
            self.previews_ready
            + self.previews_failed
            + self.previews_skipped
            + self.ai_ready
            + self.ai_needs_review
            + self.ai_failed
            + self.ai_skipped
        )


def process_pending_assets(session: Session, limit: int | None = None) -> AssetPipelineSummary:
    settings = get_settings()
    if not settings.asset_pipeline_enabled:
        return AssetPipelineSummary()

    batch_size = limit or settings.asset_pipeline_batch_size
    reset_stale_processing_assets(session)
    asset_ids = pending_pipeline_asset_ids(session, batch_size)
    claim_pipeline_assets(session, asset_ids)
    return process_asset_pipeline_batch(session, asset_ids)


def process_asset_pipeline_batch(
    session: Session,
    asset_ids: list[int],
) -> AssetPipelineSummary:
    summary = AssetPipelineSummary(selected=len(asset_ids))
    for asset_id in asset_ids:
        try:
            process_asset_pipeline(session, asset_id, summary)
        except KeyboardInterrupt:
            raise
        except Exception:
            session.rollback()
            failed_asset = session.get(Asset, asset_id)
            if failed_asset is not None:
                if failed_asset.preview_status in {"pending", "processing", "failed"}:
                    failed_asset.preview_status = "failed"
                    failed_asset.preview_error = (
                        failed_asset.preview_error or "Asset pipeline failed"
                    )
                    summary.previews_failed += 1
                else:
                    failed_asset.ai_enrichment_status = "failed"
                    failed_asset.ai_error = failed_asset.ai_error or "Asset pipeline failed"
                    summary.ai_failed += 1
                session.commit()
    return summary


def pending_pipeline_asset_ids(session: Session, limit: int) -> list[int]:
    preview_pending = Asset.preview_status == "pending"
    ai_pending = (
        (Asset.ai_enrichment_status.in_(("pending", "failed", "skipped")))
        & (Asset.preview_status == "ready")
        & or_(Asset.thumbnail_path.is_not(None), Asset.preview_path.is_not(None))
    )
    return list(
        session.scalars(
            select(Asset.id)
            .where(
                Asset.status == "active",
                Asset.source_status == "active",
                Asset.type.in_(PROCESSABLE_TYPES),
                or_(preview_pending, ai_pending),
            )
            .order_by(
                Asset.last_indexed_at.desc().nullslast(),
                Asset.created_at.desc(),
                Asset.id.desc(),
            )
            .limit(max(1, limit))
        )
    )


def claim_pipeline_assets(session: Session, asset_ids: list[int]) -> None:
    if not asset_ids:
        return
    assets = session.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    for asset in assets:
        if asset.preview_status == "pending":
            asset.preview_status = "processing"
        elif (
            asset.preview_status == "ready"
            and asset.ai_enrichment_status in {"pending", "failed", "skipped"}
        ):
            asset.ai_enrichment_status = "processing"
    session.commit()


def reset_stale_processing_assets(session: Session) -> int:
    cutoff = datetime.now(UTC) - STALE_PROCESSING_AFTER
    stale_assets = session.scalars(
        select(Asset).where(
            Asset.status == "active",
            Asset.source_status == "active",
            Asset.type.in_(PROCESSABLE_TYPES),
            Asset.updated_at < cutoff,
            or_(Asset.preview_status == "processing", Asset.ai_enrichment_status == "processing"),
        )
    ).all()
    for asset in stale_assets:
        if asset.preview_status == "processing":
            asset.preview_status = "pending"
        if asset.ai_enrichment_status == "processing":
            asset.ai_enrichment_status = "skipped"
    if stale_assets:
        session.commit()
    return len(stale_assets)


def process_asset_pipeline(
    session: Session,
    asset_id: int,
    summary: AssetPipelineSummary | None = None,
) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ValueError(f"Asset not found: {asset_id}")

    if should_generate_preview(asset):
        asset = generate_asset_preview(session, asset.id, force=preview_files_missing(asset))
        if summary is not None:
            if asset.preview_status == "ready":
                summary.previews_ready += 1
            elif asset.preview_status == "failed":
                summary.previews_failed += 1
            else:
                summary.previews_skipped += 1

    if should_run_ai(asset):
        asset = enrich_asset_with_ai(session, asset.id)
        if summary is not None:
            if asset.ai_enrichment_status == "ready":
                summary.ai_ready += 1
            elif asset.ai_enrichment_status == "needs_review":
                summary.ai_needs_review += 1
            elif asset.ai_enrichment_status == "failed":
                summary.ai_failed += 1
            else:
                summary.ai_skipped += 1
    return asset


def should_generate_preview(asset: Asset) -> bool:
    if asset.type not in PROCESSABLE_TYPES:
        return False
    if asset.preview_status == "ready" and not preview_files_missing(asset):
        return False
    return asset.preview_status in {"pending", "processing", "failed"} or preview_files_missing(asset)


def preview_files_missing(asset: Asset) -> bool:
    preview_path = local_preview_file(asset.preview_path)
    thumbnail_path = local_preview_file(asset.thumbnail_path)
    if asset.type == "image":
        return not (
            preview_path is not None
            and preview_path.is_file()
            and thumbnail_path is not None
            and thumbnail_path.is_file()
        )
    if asset.type == "video":
        return not (
            preview_path is not None
            and preview_path.is_file()
            and thumbnail_path is not None
            and thumbnail_path.is_file()
        )
    return False


def should_run_ai(asset: Asset) -> bool:
    settings = get_settings()
    if not settings.ai_enrichment_enabled:
        return False
    if asset.type not in PROCESSABLE_TYPES:
        return False
    if asset.preview_status != "ready":
        return False
    if asset.ai_enrichment_status in {"ready", "needs_review"}:
        return False
    return bool(asset.thumbnail_path or asset.preview_path)
