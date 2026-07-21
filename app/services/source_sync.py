from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, Source, SourceSyncRun
from app.services.asset_indexer import (
    ALLOWED_EXTENSIONS,
    build_embedding_text,
    build_remote_path,
    build_search_text,
    infer_asset_type,
    infer_file_ext,
    infer_filename,
    normalize_size,
    stable_asset_uid,
)
from app.services.rclone_service import RcloneError, RcloneService, sanitize_rclone_message

PLAN_GROUPS = (
    "new",
    "existing",
    "moved",
    "changed",
    "missing",
    "recovered",
    "unsupported",
    "errors",
)
INACTIVE_SOURCE_STATUSES = ("missing", "inaccessible", "deleted")


@dataclass(frozen=True)
class NormalizedRemoteFile:
    raw: dict[str, Any]
    remote_path: str
    filename: str
    file_ext: str
    remote_file_id: str | None
    size_bytes: int | None
    modified_at: datetime | None
    source_hash: str | None
    mime_type: str | None


def scan_source(
    session: Session,
    source_id: int | str,
    apply: bool = False,
    created_by: str | None = None,
) -> SourceSyncRun:
    source = resolve_source(session, source_id)
    now = datetime.now(UTC)
    run = SourceSyncRun(
        source=source,
        run_uid=f"source_sync_{uuid4().hex}",
        status="scanning",
        dry_run=not apply,
        started_at=now,
        created_by=created_by,
    )
    session.add(run)
    source.last_sync_status = "scanning"
    source.last_sync_started_at = now
    source.last_sync_completed_at = None
    source.last_sync_error = None
    session.flush()

    try:
        remote_files = RcloneService().list_json(source.rclone_remote or "", source.root_path or "")
        plan = build_source_sync_plan(session, source, remote_files)
        summary = summarize_plan(plan, total_remote=len(remote_files))
        run.summary_json = summary
        run.report_json = plan
        if apply:
            apply_source_sync_plan(session, source, plan, run)
            run.status = "partial" if summary.get("errors") else "applied"
            run.dry_run = False
        else:
            run.status = "dry_run_ready"
        completed = datetime.now(UTC)
        run.completed_at = completed
        source.last_sync_status = run.status
        source.last_sync_completed_at = completed
        source.last_sync_summary = summary
        session.commit()
    except Exception as exc:
        session.rollback()
        source = resolve_source(session, source_id)
        run = SourceSyncRun(
            source=source,
            run_uid=f"source_sync_{uuid4().hex}",
            status="failed",
            dry_run=not apply,
            started_at=now,
            completed_at=datetime.now(UTC),
            error=sanitize_error(exc),
            created_by=created_by,
        )
        source.last_sync_status = "failed"
        source.last_sync_completed_at = run.completed_at
        source.last_sync_error = run.error
        session.add(run)
        session.commit()
    return run


def build_source_sync_plan(
    session: Session,
    source: Source,
    remote_files: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    plan: dict[str, list[dict[str, Any]]] = {group: [] for group in PLAN_GROUPS}
    assets = session.scalars(select(Asset).where(Asset.source_id == source.id)).all()
    by_file_id = {asset.remote_file_id: asset for asset in assets if asset.remote_file_id}
    by_path = {asset.remote_path: asset for asset in assets}
    seen_asset_ids: set[int] = set()

    for entry in remote_files:
        if entry.get("IsDir"):
            continue
        try:
            remote = normalize_remote_file(source, entry)
        except Exception as exc:
            plan["errors"].append({"action": "error", "reason": sanitize_error(exc)})
            continue
        if remote.file_ext not in ALLOWED_EXTENSIONS:
            plan["unsupported"].append(
                report_item("unsupported", remote, reason="unsupported extension")
            )
            continue

        asset = by_file_id.get(remote.remote_file_id) if remote.remote_file_id else None
        matched_by = "remote_file_id" if asset is not None else None
        if asset is None:
            asset = by_path.get(remote.remote_path)
            matched_by = "remote_path" if asset is not None else None

        if asset is None:
            plan["new"].append(report_item("new", remote))
            continue

        seen_asset_ids.add(asset.id)
        item = report_item("existing", remote, asset=asset, reason=f"matched by {matched_by}")
        if is_missing_source_asset(asset):
            item["action"] = "recovered"
            plan["recovered"].append(item)
            continue
        if (
            remote.remote_file_id
            and asset.remote_file_id == remote.remote_file_id
            and asset.remote_path != remote.remote_path
        ):
            item["action"] = "moved"
            item["previous_remote_path"] = asset.remote_path
            plan["moved"].append(item)
            continue
        if file_changed(asset, remote):
            item["action"] = "changed"
            item["reason"] = "size, modified time, or hash changed"
            plan["changed"].append(item)
            continue
        plan["existing"].append(item)

    for asset in assets:
        if asset.id in seen_asset_ids:
            continue
        if is_missing_source_asset(asset):
            continue
        plan["missing"].append(
            {
                "action": "missing",
                "filename": asset.filename,
                "remote_path": asset.remote_path,
                "asset_id": asset.id,
                "reason": "asset not found in remote listing",
            }
        )
    return plan


def apply_source_sync_plan(
    session: Session,
    source: Source,
    plan: dict[str, list[dict[str, Any]]],
    run: SourceSyncRun,
) -> None:
    now = datetime.now(UTC)
    for item in plan.get("new", []):
        remote = item["remote"]
        asset = Asset(
            asset_uid=stable_asset_uid(source.source_id, remote["remote_path"]),
            source=source,
            provider=source.provider,
            rclone_remote=source.rclone_remote,
            remote_path=remote["remote_path"],
            source_path=remote["remote_path"],
            drive_file_id=remote["remote_file_id"],
            remote_file_id=remote["remote_file_id"],
            filename=remote["filename"],
            file_ext=remote["file_ext"],
            mime_type=remote["mime_type"],
            type=infer_asset_type(remote["file_ext"]),
            size_bytes=remote["size_bytes"],
            source_size_bytes=remote["size_bytes"],
            source_modified_at=parse_datetime(remote["modified_at"]),
            source_hash=remote["source_hash"],
            status="active",
            source_status="active",
            source_last_seen_at=now,
            last_indexed_at=now,
            preview_status="pending",
            technical_metadata_status="pending",
            ai_enrichment_status="pending",
        )
        asset.search_text = build_search_text(
            asset,
            source=source,
            brand=None,
            product=None,
            niche=None,
        )
        asset.embedding_text = build_embedding_text(
            asset,
            source=source,
            brand=None,
            product=None,
            niche=None,
        )
        session.add(asset)

    for group in ("existing", "moved", "changed", "recovered"):
        for item in plan.get(group, []):
            asset = session.get(Asset, item["asset_id"])
            if asset is None:
                continue
            remote = item["remote"]
            if group == "changed":
                asset.preview_status = "pending"
                asset.technical_metadata_status = "pending"
                asset.ai_enrichment_status = "pending"
                asset.preview_error = None
                asset.probe_error = None
                asset.ai_error = None
            asset.remote_path = remote["remote_path"]
            asset.source_path = remote["remote_path"]
            asset.rclone_remote = source.rclone_remote
            asset.remote_file_id = asset.remote_file_id or remote["remote_file_id"]
            asset.drive_file_id = asset.drive_file_id or remote["remote_file_id"]
            asset.filename = remote["filename"]
            asset.file_ext = remote["file_ext"]
            asset.mime_type = remote["mime_type"]
            asset.size_bytes = remote["size_bytes"]
            asset.source_size_bytes = remote["size_bytes"]
            asset.source_modified_at = parse_datetime(remote["modified_at"])
            asset.source_hash = remote["source_hash"]
            asset.source_status = "active"
            asset.source_last_seen_at = now
            asset.source_missing_at = None
            asset.last_indexed_at = now

    for item in plan.get("missing", []):
        asset = session.get(Asset, item["asset_id"])
        if asset is None:
            continue
        asset.source_status = "missing"
        asset.source_missing_at = now
    run.report_json = plan


def resolve_source(session: Session, source_id: int | str) -> Source:
    if isinstance(source_id, int) or str(source_id).isdigit():
        source = session.get(Source, int(source_id))
    else:
        source = session.scalar(select(Source).where(Source.source_id == str(source_id)))
    if source is None:
        raise ValueError(f"Source not found: {source_id}")
    if not source.sync_enabled:
        raise ValueError(f"Source sync is disabled: {source.source_id}")
    if not source.rclone_remote:
        raise ValueError(f"Source has no rclone remote: {source.source_id}")
    return source


def normalize_remote_file(source: Source, entry: dict[str, Any]) -> NormalizedRemoteFile:
    filename = infer_filename(entry)
    file_ext = infer_file_ext(entry)
    return NormalizedRemoteFile(
        raw=entry,
        remote_path=build_remote_path(source.root_path or "", entry),
        filename=filename,
        file_ext=file_ext,
        remote_file_id=clean_string(entry.get("ID")),
        size_bytes=normalize_size(entry.get("Size")),
        modified_at=parse_datetime(entry.get("ModTime")),
        source_hash=extract_hash(entry.get("Hashes")),
        mime_type=clean_string(entry.get("MimeType")),
    )


def report_item(
    action: str,
    remote: NormalizedRemoteFile,
    asset: Asset | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "filename": remote.filename,
        "remote_path": remote.remote_path,
        "asset_id": asset.id if asset else None,
        "reason": reason,
        "remote": serialize_remote(remote),
    }


def serialize_remote(remote: NormalizedRemoteFile) -> dict[str, Any]:
    return {
        "remote_path": remote.remote_path,
        "filename": remote.filename,
        "file_ext": remote.file_ext,
        "remote_file_id": remote.remote_file_id,
        "size_bytes": remote.size_bytes,
        "modified_at": remote.modified_at.isoformat() if remote.modified_at else None,
        "source_hash": remote.source_hash,
        "mime_type": remote.mime_type,
    }


def summarize_plan(plan: dict[str, list[dict[str, Any]]], total_remote: int) -> dict[str, int]:
    summary = {"total_remote": total_remote}
    for group in PLAN_GROUPS:
        summary[group] = len(plan.get(group, []))
    return summary


def file_changed(asset: Asset, remote: NormalizedRemoteFile) -> bool:
    if (
        asset.source_size_bytes is not None
        and remote.size_bytes is not None
        and asset.source_size_bytes != remote.size_bytes
    ):
        return True
    if (
        asset.source_modified_at is not None
        and remote.modified_at is not None
        and asset.source_modified_at != remote.modified_at
    ):
        return True
    return bool(
        asset.source_hash
        and remote.source_hash
        and asset.source_hash != remote.source_hash
    )


def is_missing_source_asset(asset: Asset) -> bool:
    return asset.source_status in {"missing", "inaccessible", "deleted"}


def extract_hash(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("SHA-1", "sha1", "MD5", "md5"):
        hash_value = value.get(key)
        if isinstance(hash_value, str) and hash_value:
            return hash_value
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def clean_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def extract_drive_folder_id(folder_url: str | None) -> str | None:
    if not folder_url:
        return None
    patterns = [
        r"/folders/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, folder_url)
        if match:
            return match.group(1)
    candidate = folder_url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", candidate):
        return candidate
    return None


def sanitize_error(exc: Exception) -> str:
    if isinstance(exc, RcloneError):
        return str(exc)
    return sanitize_rclone_message(str(exc))
