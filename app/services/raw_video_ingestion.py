from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Asset, RawVideo
from app.services.asset_indexer import VIDEO_EXTENSIONS
from app.services.long_video_segmentation import (
    LongVideoSegmentationError,
    sanitize_segmentation_error,
    segment_long_video_asset,
    validate_rclone_remote_exists,
)
from app.services.rclone_service import RcloneService

DEFAULT_RAW_VIDEO_PROVIDER = "gdrive_people_raw_long"


@dataclass(frozen=True)
class RawVideoSyncSummary:
    videos_found: int = 0
    videos_new: int = 0
    videos_existing: int = 0


@dataclass
class RawVideoProcessSummary:
    videos_found: int = 0
    videos_new: int = 0
    videos_processed: int = 0
    videos_skipped: int = 0
    videos_failed: int = 0
    segments_generated: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)


def sync_raw_videos(
    session: Session,
    remote: str = DEFAULT_RAW_VIDEO_PROVIDER,
    root: str = "",
    provider: str = DEFAULT_RAW_VIDEO_PROVIDER,
    rclone: RcloneService | None = None,
) -> RawVideoSyncSummary:
    entries = (rclone or RcloneService()).list_json(remote, root)
    now = datetime.now(UTC)
    found = 0
    created = 0
    existing = 0

    for entry in entries:
        remote_path = raw_video_remote_path(root, entry)
        if not remote_path or not is_video_path(remote_path):
            continue
        found += 1
        raw_video = session.scalar(select(RawVideo).where(RawVideo.remote_path == remote_path))
        if raw_video is None:
            raw_video = RawVideo(
                remote_path=remote_path,
                provider=provider,
                status="NEW",
                size_bytes=normalize_size(entry.get("Size")),
                duration_seconds=normalize_duration(entry),
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(raw_video)
            created += 1
        else:
            raw_video.provider = raw_video.provider or provider
            raw_video.size_bytes = normalize_size(entry.get("Size"))
            raw_video.duration_seconds = raw_video.duration_seconds or normalize_duration(entry)
            raw_video.last_seen_at = now
            raw_video.updated_at = now
            existing += 1

    session.commit()
    return RawVideoSyncSummary(videos_found=found, videos_new=created, videos_existing=existing)


def process_raw_videos(
    session: Session,
    status: str = "NEW",
    limit: int = 10,
    created_by: str | None = None,
    derived_remote: str | None = None,
    derived_root: str | None = None,
    skip_ai: bool = False,
    resume: bool = False,
) -> RawVideoProcessSummary:
    if resume:
        mark_interrupted_processing(session)
    if derived_remote:
        validate_rclone_remote_exists(derived_remote, role="derived")

    raw_videos = session.scalars(
        select(RawVideo)
        .where(RawVideo.status == status)
        .order_by(RawVideo.created_at.asc(), RawVideo.id.asc())
        .limit(max(1, limit))
    ).all()
    summary = RawVideoProcessSummary(videos_found=len(raw_videos))

    for raw_video in raw_videos:
        claimed = claim_raw_video(session, raw_video.id, expected_status=status)
        if claimed is None:
            summary.videos_skipped += 1
            continue
        try:
            parent = find_parent_asset(session, claimed.remote_path)
            if parent is None:
                raise LongVideoSegmentationError(f"Indexed parent asset not found: {claimed.remote_path}")
            run = segment_long_video_asset(
                session=session,
                asset_id=parent.id,
                force=False,
                created_by=created_by,
                derived_remote=derived_remote,
                derived_root=derived_root,
                skip_ai=skip_ai,
                raw_video_id=claimed.id,
            )
            report = run.report_json if isinstance(run.report_json, dict) else {}
            created = int(report.get("children_created") or 0)
            if report.get("skipped") and report.get("reason") == "already_processed":
                created = link_assets_for_raw_video(session, claimed)
            if getattr(run, "status", None) == "failed" or (created == 0 and not report.get("skipped")):
                message = getattr(run, "error", None) or "Segmentation finished without generated assets"
                mark_raw_video_failed(session, claimed.id, message)
                summary.videos_failed += 1
                summary.runs.append({"raw_video_id": claimed.id, "run_id": run.id, "report": report})
                continue
            mark_raw_video_done(session, claimed.id, segments_generated=created)
            summary.videos_processed += 1
            summary.segments_generated += created
            summary.runs.append({"raw_video_id": claimed.id, "run_id": run.id, "report": report})
        except Exception as exc:
            session.rollback()
            mark_raw_video_failed(session, raw_video.id, str(exc))
            summary.videos_failed += 1

    return summary


def claim_raw_video(session: Session, raw_video_id: int, expected_status: str = "NEW") -> RawVideo | None:
    now = datetime.now(UTC)
    result = session.execute(
        update(RawVideo)
        .where(RawVideo.id == raw_video_id, RawVideo.status == expected_status)
        .values(status="PROCESSING", last_error=None, updated_at=now)
        .returning(RawVideo.id)
    ).first()
    session.commit()
    if result is None:
        return None
    return session.get(RawVideo, result[0])


def mark_raw_video_done(session: Session, raw_video_id: int, segments_generated: int) -> RawVideo | None:
    raw_video = session.get(RawVideo, raw_video_id)
    if raw_video is None:
        return None
    raw_video.status = "DONE"
    raw_video.segments_generated = segments_generated
    raw_video.processed_at = datetime.now(UTC)
    raw_video.last_error = None
    raw_video.updated_at = raw_video.processed_at
    session.commit()
    return raw_video


def mark_raw_video_failed(session: Session, raw_video_id: int, message: str) -> RawVideo | None:
    raw_video = session.get(RawVideo, raw_video_id)
    if raw_video is None:
        return None
    raw_video.status = "FAILED"
    raw_video.processed_at = None
    raw_video.last_error = sanitize_segmentation_error(message)
    raw_video.updated_at = datetime.now(UTC)
    session.commit()
    return raw_video


def mark_interrupted_processing(session: Session) -> int:
    now = datetime.now(UTC)
    raw_videos = session.scalars(select(RawVideo).where(RawVideo.status == "PROCESSING")).all()
    for raw_video in raw_videos:
        raw_video.status = "FAILED"
        raw_video.processed_at = None
        raw_video.last_error = "Interrupted processing"
        raw_video.updated_at = now
    session.commit()
    return len(raw_videos)


def raw_video_status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(RawVideo.status, func.count(RawVideo.id)).group_by(RawVideo.status)).all()
    counts = {"NEW": 0, "PROCESSING": 0, "DONE": 0, "FAILED": 0}
    counts.update({status: int(count) for status, count in rows})
    return counts


def find_parent_asset(session: Session, remote_path: str) -> Asset | None:
    return session.scalar(
        select(Asset)
        .where(
            Asset.remote_path == remote_path,
            Asset.type == "video",
            Asset.is_derivative.is_(False),
        )
        .order_by(Asset.id.asc())
    )


def link_assets_for_raw_video(session: Session, raw_video: RawVideo) -> int:
    assets = session.scalars(select(Asset).where(Asset.source_video == raw_video.remote_path)).all()
    for asset in assets:
        asset.raw_video_id = raw_video.id
    session.commit()
    return len(assets)


def raw_video_remote_path(root: str, entry: dict[str, Any]) -> str:
    path = str(entry.get("Path") or entry.get("Name") or "").strip("/")
    if not path:
        return ""
    clean_root = root.strip("/")
    if clean_root and not path.startswith(f"{clean_root}/"):
        return str(PurePosixPath(clean_root) / path)
    return path


def is_video_path(remote_path: str) -> bool:
    suffix = PurePosixPath(remote_path).suffix.lower().lstrip(".")
    return suffix in VIDEO_EXTENSIONS


def normalize_size(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_duration(entry: dict[str, Any]) -> float | None:
    value = entry.get("Duration") or entry.get("duration_seconds")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
