from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol
from urllib import parse, request

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Asset, AssetAIAnalysis, Brand, ManagedDriveFolder, Source
from app.models.asset import ASSET_COLLECTION_VALUES
from app.schemas.ai_enrichment import AIAssetEnrichmentResult
from app.services.ai_asset_enrichment import call_openai_vision
from app.services.ai_prompts import ASSET_ENRICHMENT_PROMPT_VERSION, build_asset_enrichment_prompt
from app.services.asset_preview import (
    AssetPreviewError,
    ParsedProbeMetadata,
    parse_ffprobe_metadata,
    run_ffprobe,
    run_ffmpeg,
    sanitize_error_message,
)
from app.services.ai_providers.nvidia_provider import NvidiaProviderError, call_nvidia_vision
from app.services.ai_providers.openai_provider import sanitize_provider_error
from app.services.managed_drive.naming import (
    aspect_ratio,
    clean_semantic_slug,
    compact_orientation,
    humanize_slug,
    near as near,
    normalize_existing_enum,
    pilot_orientation,
    safe_original_filename,
    sanitized_list,
    short_id,
    shot_type_es,
    slugify,
    split_text_list,
    text_or_none,
    trim_semantic_stem,
)
from app.services.managed_drive.layout import (
    compact_folder_primary_topic,
    folder_context_slug,
    legacy_context_slug,
    route_parts,
)
from app.services.managed_drive.contracts import (
    BatchRequest,
    BatchScope,
    Classification,
    DriveFile,
    IngestRequest,
    ManagedAIResult,
    PreflightRequest,
    PreviewResult,
    ReviewApproval,
    TechnicalResult,
)
from app.services.managed_drive.results import (
    BatchAssetResult,
    BatchCounters,
    BatchResult,
    DriveStatusResult,
    DriveStatusRow,
    IngestPlan,
    LegacyLayoutAssetPlan,
    LegacyLayoutMigrationResult,
    MutationStats,
    PreflightResult,
)
from app.services.people_metadata import (
    gendered_metadata_allowed,
    neutralize_gendered_tags,
    neutralize_gendered_terms,
    people_search_terms,
    source_presentation_hint,
)
from app.services.rclone_service import rclone_config_path, sanitize_rclone_message

CATALOG_VERSION = "managed_drive_pilot_v1"
SOURCE_ID = "managed-drive-pilot"
PRIMARY_THEMES = {
    "personas",
    "naturaleza",
    "animales",
    "ciudad-arquitectura",
    "hogar-interiores",
    "oficina-negocios",
    "tecnologia",
    "salud-bienestar",
    "alimentos",
    "transporte",
    "fondos-texturas",
    "abstractos",
    "objetos",
    "otros",
}
REVIEW_PATH = "90_revision/clasificacion-ambigua"
PATH_LAYOUT_VERSION = "compact_v2"
SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/heic"}
SUPPORTED_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/x-matroska", "video/webm"}
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
PLAN_VERSION = "managed_drive_plan_v2"
PLAN_HASH_FIELDS = (
    "drive_file_id",
    "original_name",
    "original_parent_id",
    "scope",
    "mime_type",
    "media_type",
    "source_size_bytes",
    "primary_theme",
    "primary_topic",
    "orientation",
    "target_name",
    "target_path",
    "target_batch",
    "path_layout_version",
    "brand_id",
    "title_slug",
    "title_type",
    "collection",
)


class ManagedDriveError(RuntimeError):
    error_type = "managed_drive_error"


class DriveAuthError(ManagedDriveError):
    error_type = "drive_auth_error"


class DriveFileNotFoundError(ManagedDriveError):
    error_type = "drive_file_not_found"


class MoveError(ManagedDriveError):
    error_type = "move_error"


class LegacyLayoutMigrationError(ManagedDriveError):
    error_type = "legacy_layout_migration_error"


class DriveClient(Protocol):
    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]: ...
    def get_file_metadata(self, file_id: str) -> DriveFile: ...
    def download_file(self, file_id: str, local_path: Path) -> None: ...
    def create_folder(self, parent_id: str, name: str) -> str: ...
    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile: ...
    def restore_file_location(self, file_id: str, original_name: str, original_parent_id: str) -> DriveFile: ...
    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool: ...
    def trash_file(self, file_id: str) -> DriveFile: ...
    def untrash_file(self, file_id: str) -> DriveFile: ...
    def folder_exists(self, folder_id: str) -> bool: ...


class BatchLockUnavailable(ManagedDriveError):
    error_type = "batch_lock_unavailable"


@dataclass(frozen=True)
class ReviewedReplanResult:
    dry_run: bool
    file_id: str
    changed: bool
    skipped_reason: str | None
    target_name_before: str | None
    target_name_after: str | None
    target_path_before: str | None
    target_path_after: str | None
    plan_hash_before: str | None
    plan_hash_after: str | None
    status_before: str | None
    status_after: str | None
    move_status_before: str | None
    move_status_after: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "file_id": self.file_id,
            "changed": self.changed,
            "skipped_reason": self.skipped_reason,
            "target_name_before": self.target_name_before,
            "target_name_after": self.target_name_after,
            "target_path_before": self.target_path_before,
            "target_path_after": self.target_path_after,
            "plan_hash_before": self.plan_hash_before,
            "plan_hash_after": self.plan_hash_after,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "move_status_before": self.move_status_before,
            "move_status_after": self.move_status_after,
        }


@dataclass(frozen=True)
class MovedRenameResult:
    dry_run: bool
    file_id: str
    changed: bool
    skipped_reason: str | None
    drive_file_id_before: str | None
    drive_file_id_after: str | None
    parent_before: str | None
    parent_after: str | None
    target_name_before: str | None
    target_name_after: str | None
    target_path_before: str | None
    target_path_after: str | None
    plan_hash_before: str | None
    plan_hash_after: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "file_id": self.file_id,
            "changed": self.changed,
            "skipped_reason": self.skipped_reason,
            "drive_file_id_before": self.drive_file_id_before,
            "drive_file_id_after": self.drive_file_id_after,
            "parent_before": self.parent_before,
            "parent_after": self.parent_after,
            "target_name_before": self.target_name_before,
            "target_name_after": self.target_name_after,
            "target_path_before": self.target_path_before,
            "target_path_after": self.target_path_after,
            "plan_hash_before": self.plan_hash_before,
            "plan_hash_after": self.plan_hash_after,
        }


class NvidiaBatchAI:
    def __init__(self, model: str, pause_seconds: float = 0.75) -> None:
        self.model = model
        self.pause_seconds = pause_seconds
        self.requests = 0
        self.retries = 0

    def __call__(self, asset: Asset, path: Path, previews: PreviewResult) -> ManagedAIResult:
        if self.requests:
            time.sleep(self.pause_seconds)
        image_paths = pilot_ai_image_inputs(asset, path, previews)
        prompt = build_asset_enrichment_prompt(asset)
        try:
            return self._call(prompt, image_paths)
        except NvidiaProviderError as exc:
            if not nvidia_error_is_retryable(exc):
                if nvidia_error_is_invalid_json(exc):
                    return self.invalid_json_result(asset)
                raise typed_error("ai_error", sanitize_provider_error(str(exc))) from exc
            self.retries += 1
            time.sleep(1.5)
            try:
                return self._call(prompt, image_paths)
            except NvidiaProviderError as retry_exc:
                if nvidia_error_is_invalid_json(retry_exc):
                    return self.invalid_json_result(asset)
                raise typed_error("ai_error", sanitize_provider_error(str(retry_exc))) from retry_exc

    def _call(self, prompt: str, image_paths: list[Path]) -> ManagedAIResult:
        self.requests += 1
        result = call_nvidia_vision(prompt, image_paths, model=self.model)
        return managed_result_from_existing(
            result,
            provider="nvidia",
            model=self.model,
            frames_analyzed=count_frame_inputs(image_paths),
        )

    def invalid_json_result(self, asset: Asset) -> ManagedAIResult:
        return ManagedAIResult(
            title_es=asset.filename,
            description_es=f"Respuesta NVIDIA invalida para {asset.filename}.",
            primary_theme="otros",
            primary_topic=None,
            tags=[],
            tags_source=None,
            suggested_uses=["revision"],
            confidence={"overall": 0.0},
            requires_review=True,
            warnings=["ai_response_invalid"],
            ai_analysis_mode="REAL",
            ai_provider="nvidia",
            ai_model=self.model,
            frames_analyzed=0,
        )


def validate_ingest_request(request_data: IngestRequest) -> None:
    if request_data.scope not in {"generic", "brand", "title"}:
        raise ValueError("scope must be one of: generic, brand, title")
    if request_data.limit < 1:
        raise ValueError("limit must be at least 1")
    if request_data.apply == request_data.dry_run:
        raise ValueError("--dry-run and --apply are mutually exclusive")
    if request_data.scope == "generic":
        if request_data.brand_slug or request_data.title or request_data.title_type:
            raise ValueError("generic scope does not accept brand or title parameters")
    if request_data.scope == "brand":
        if not request_data.brand_slug:
            raise ValueError("brand scope requires --brand")
        if request_data.collection not in ASSET_COLLECTION_VALUES:
            raise ValueError("collection must be one of: evergreen, campaign, ugc, brand-kit")
    if request_data.scope == "title":
        if not request_data.title:
            raise ValueError("title scope requires --title")
        if request_data.title_type not in {"movie", "series"}:
            raise ValueError("title scope requires --title-type movie|series")
        if request_data.title_type == "movie" and (request_data.season or request_data.episode):
            raise ValueError("movie scope does not accept season or episode")


def ingest_drive_assets(
    session: Session,
    request_data: IngestRequest,
    drive_client: DriveClient | None = None,
    technical_analyzer: Any | None = None,
    preview_generator: Any | None = None,
    ai_enricher: Any | None = None,
    mutation_client: DriveClient | None = None,
) -> list[IngestPlan]:
    validate_ingest_request(request_data)
    drive_client = drive_client or drive_client_from_environment()
    mutation_client = mutation_client or (
        drive_client
        if request_data.dry_run or request_data.simulate_drive_mutation
        else mutation_client_for_apply(drive_client)
    )
    technical_analyzer = technical_analyzer or analyze_technical_metadata
    preview_generator = preview_generator or generate_pilot_previews
    ai_enricher = ai_enricher or enrich_with_existing_ai_system
    if request_data.apply and request_data.file_id:
        idempotent_plan = resolve_applied_idempotence(session, request_data, mutation_client)
        if idempotent_plan is not None:
            return [idempotent_plan]
    validate_pilot_folders(drive_client, request_data)
    if request_data.apply:
        preflight = preflight_drive_access(
            PreflightRequest(
                request_data.source_folder_id,
                request_data.destination_root_id,
                request_data.file_id,
            ),
            drive_client,
        )
        if not preflight.scopes_complete:
            raise DriveAuthError("SCOPES_COMPLETE=NO; apply is blocked")

    files = (
        [request_file_metadata(drive_client, request_data)]
        if request_data.file_id
        else drive_client.list_folder(request_data.source_folder_id, request_data.limit)
    )
    plans: list[IngestPlan] = []
    for drive_file in files[: request_data.limit]:
        try:
            plan = process_drive_file(
                session,
                request_data,
                drive_client,
                mutation_client,
                drive_file,
                technical_analyzer,
                preview_generator,
                ai_enricher,
            )
        except Exception as exc:
            session.rollback()
            plan = failed_plan(drive_file, request_data, exc)
        plans.append(plan)
    return plans


def default_batch_scopes() -> list[BatchScope]:
    settings = get_settings()
    missing = []
    required = {
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": settings.google_drive_root_folder_id,
        "GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID": settings.google_drive_generic_inbox_folder_id,
        "GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID": settings.google_drive_brand_inbox_folder_id,
        "GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID": settings.google_drive_title_inbox_folder_id,
    }
    for key, value in required.items():
        if not value:
            missing.append(key)
    if missing:
        raise ValueError("missing Drive pilot settings: " + ", ".join(missing))
    assert settings.google_drive_root_folder_id is not None
    assert settings.google_drive_generic_inbox_folder_id is not None
    assert settings.google_drive_brand_inbox_folder_id is not None
    assert settings.google_drive_title_inbox_folder_id is not None
    return [
        BatchScope(
            scope="generic",
            source_folder_id=settings.google_drive_generic_inbox_folder_id,
            destination_root_id=settings.google_drive_root_folder_id,
        ),
        BatchScope(
            scope="brand",
            source_folder_id=settings.google_drive_brand_inbox_folder_id,
            destination_root_id=settings.google_drive_root_folder_id,
            brand_slug="grandiosa-mujer",
            collection="evergreen",
        ),
        BatchScope(
            scope="title",
            source_folder_id=settings.google_drive_title_inbox_folder_id,
            destination_root_id=settings.google_drive_root_folder_id,
            title_type="series",
            title="Mi otra yo",
        ),
    ]


def run_drive_batch(
    session: Session,
    request_data: BatchRequest,
    drive_client: DriveClient | None = None,
    technical_analyzer: Any | None = None,
    preview_generator: Any | None = None,
    ai_enricher: Any | None = None,
    mutation_client: DriveClient | None = None,
    scopes: list[BatchScope] | None = None,
) -> BatchResult:
    if request_data.max_total < 1:
        raise ValueError("max_total must be at least 1")
    if request_data.max_per_scope < 1:
        raise ValueError("max_per_scope must be at least 1")
    if request_data.concurrency != 1:
        raise ValueError("pilot batch concurrency must be 1")

    started_at = datetime.now(UTC)
    counters = BatchCounters()
    assets: list[BatchAssetResult] = []
    if not acquire_batch_lock(session):
        finished_at = datetime.now(UTC)
        return BatchResult(
            run_mode="apply" if request_data.apply else "dry-run",
            started_at=started_at,
            finished_at=finished_at,
            concurrency=request_data.concurrency,
            lock_acquired=False,
            counters=counters,
            assets=[],
        )

    drive_client = drive_client or drive_client_from_environment()
    mutation_client = mutation_client or (
        mutation_client_for_apply(drive_client) if request_data.apply else None
    )
    scopes = scopes or default_batch_scopes()
    if request_data.apply:
        try:
            return run_drive_batch_apply(
                session,
                request_data,
                drive_client,
                mutation_client,
                scopes,
                started_at,
                counters,
                assets,
            )
        finally:
            release_batch_lock(session)

    nvidia_ai = ai_enricher or NvidiaBatchAI(
        model="nvidia/nemotron-nano-12b-v2-vl",
        pause_seconds=request_data.nvidia_pause_seconds,
    )
    seen: set[str] = set()

    try:
        for scope_config in scopes:
            remaining_total = request_data.max_total - counters.files_new
            if remaining_total <= 0:
                break
            listed = drive_client.list_folder(
                scope_config.source_folder_id,
                max(request_data.max_per_scope, remaining_total),
            )
            counters.files_discovered += len(listed)
            per_scope_new = 0
            for drive_file in listed:
                if counters.files_new >= request_data.max_total or per_scope_new >= request_data.max_per_scope:
                    break
                if drive_file.id in seen:
                    counters.duplicates += 1
                    counters.files_skipped += 1
                    assets.append(skipped_batch_asset(drive_file, scope_config, "duplicate_in_listing"))
                    continue
                seen.add(drive_file.id)
                existing = session.scalar(select(Asset).where(Asset.drive_file_id == drive_file.id))
                if existing and existing.status in {"ready", "review_required"} and existing.move_status == "moved":
                    counters.files_skipped += 1
                    assets.append(skipped_existing_asset(existing, drive_file, scope_config))
                    continue

                counters.files_new += 1
                per_scope_new += 1
                request_for_file = batch_ingest_request(scope_config, drive_file.id, request_data.apply)
                before_requests = getattr(nvidia_ai, "requests", 0)
                before_retries = getattr(nvidia_ai, "retries", 0)
                plan = ingest_drive_assets(
                    session,
                    request_for_file,
                    drive_client,
                    technical_analyzer,
                    preview_generator,
                    nvidia_ai,
                    mutation_client,
                )[0]
                counters.nvidia_requests += getattr(nvidia_ai, "requests", 0) - before_requests
                counters.nvidia_retries += getattr(nvidia_ai, "retries", 0) - before_retries
                if plan.ai_call:
                    counters.files_analyzed += 1
                if plan.result == "applied":
                    counters.files_applied += 1
                if plan.status == "ready":
                    counters.files_ready += 1
                if plan.status == "review_required":
                    counters.files_sent_to_review += 1
                if plan.status == "failed" or plan.result in {"move_failed"}:
                    counters.files_failed += 1
                counters.drive_mutations_attempted += int(
                    (plan.mutation_stats or {}).get("mutations_attempted", 0)
                )
                counters.drive_mutations_succeeded += int(
                    (plan.mutation_stats or {}).get("mutations_succeeded", 0)
                )
                assets.append(batch_asset_from_plan(plan, session))
        finished_at = datetime.now(UTC)
        return BatchResult(
            run_mode="apply" if request_data.apply else "dry-run",
            started_at=started_at,
            finished_at=finished_at,
            concurrency=request_data.concurrency,
            lock_acquired=True,
            counters=counters,
            assets=assets,
        )
    finally:
        release_batch_lock(session)
        if hasattr(nvidia_ai, "requests"):
            counters.nvidia_requests += max(0, getattr(nvidia_ai, "requests", 0) - counters.nvidia_requests)


def run_drive_batch_apply(
    session: Session,
    request_data: BatchRequest,
    drive_client: DriveClient,
    mutation_client: DriveClient | None,
    scopes: list[BatchScope],
    started_at: datetime,
    counters: BatchCounters,
    assets: list[BatchAssetResult],
) -> BatchResult:
    assert mutation_client is not None
    plans_to_apply = apply_candidates(session, request_data, scopes)
    validate_apply_candidates(request_data, plans_to_apply)
    counters.plans_selected = len(plans_to_apply)

    for asset, _scope_config in plans_to_apply:
        assert asset is not None
        request_for_asset = replace(ingest_request_from_asset(asset), apply=True, dry_run=False)
        if moved_asset_can_noop(asset):
            plan = resolve_applied_idempotence(session, request_for_asset, drive_client)
            if plan is None:
                raise ValueError(f"--only-file-id is not apply-eligible: {asset.drive_file_id}")
        else:
            assert_persisted_plan_hash(asset)
            plan = plan_from_persisted_asset(
                asset,
                request_for_asset,
                DriveFile(
                    id=asset.drive_file_id or "",
                    name=asset.original_name or asset.filename,
                    mime_type=asset.mime_type,
                    parents=[asset.original_parent_id] if asset.original_parent_id else [],
                    size=asset.source_size_bytes,
                    modified_time=asset.source_modified_at,
                ),
            )
            target_parts = str(asset.remote_path).split("/")[:-1]
            plan = apply_move(session, mutation_client, asset, request_for_asset, target_parts, plan)
        if plan.result == "applied":
            counters.files_applied += 1
        if plan.result == "already_applied":
            counters.already_applied += 1
            counters.files_skipped += 1
        if plan.status == "ready":
            counters.files_ready += 1
        if plan.status == "review_required":
            counters.files_sent_to_review += 1
        if plan.status == "failed" or plan.result in {"move_failed"}:
            counters.files_failed += 1
        counters.drive_mutations_attempted += int((plan.mutation_stats or {}).get("mutations_attempted", 0))
        counters.drive_mutations_succeeded += int((plan.mutation_stats or {}).get("mutations_succeeded", 0))
        assets.append(batch_asset_from_plan(plan, session))
        if plan.status == "failed" or plan.result in {"move_failed"}:
            break

    finished_at = datetime.now(UTC)
    return BatchResult(
        run_mode="apply",
        started_at=started_at,
        finished_at=finished_at,
        concurrency=request_data.concurrency,
        lock_acquired=True,
        counters=counters,
        assets=assets,
    )


def list_drive_status(
    session: Session,
    status: str | None = None,
    scope: str | None = None,
    limit: int = 100,
) -> DriveStatusResult:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    query = select(Asset).where(Asset.drive_file_id.is_not(None)).order_by(Asset.updated_at.desc(), Asset.id.desc())
    if status:
        query = query.where(Asset.status == status)
    if scope:
        query = query.where(Asset.scope == scope)
    rows = [drive_status_row(asset) for asset in session.scalars(query.limit(limit)).all()]
    return DriveStatusResult(rows=rows, summary=drive_status_summary(session))


def drive_status_row(asset: Asset) -> DriveStatusRow:
    return DriveStatusRow(
        drive_file_id=asset.drive_file_id,
        scope=asset.scope,
        original_name=asset.original_name,
        current_name=asset.filename,
        status=asset.status,
        move_status=asset.move_status,
        requires_review=asset.status == "review_required" or asset.needs_human_review,
        review_reason=asset.review_reason,
        provider=asset.provider,
        model=asset.ai_model,
        target_path=asset.remote_path,
        path_layout_version=asset.path_layout_version,
        updated_at=asset.updated_at,
    )


def drive_status_summary(session: Session) -> dict[str, int]:
    return {
        "ready": count_assets(session, Asset.status == "ready"),
        "planned": count_assets(session, Asset.status == "move_planned"),
        "review": count_assets(session, Asset.status == "review_required"),
        "failed": count_assets(session, Asset.status == "failed"),
        "pending_analysis": count_assets(
            session,
            Asset.status.in_(
                [
                    "discovered",
                    "downloading",
                    "technical_analysis",
                    "preview_generation",
                    "ai_analysis",
                ]
            ),
        ),
        "duplicates": count_duplicate_drive_file_ids(session),
    }


def count_assets(session: Session, criterion: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(Asset).where(criterion)) or 0)


def list_review_required_assets(session: Session, limit: int = 100) -> DriveStatusResult:
    return list_drive_status(session, status="review_required", limit=limit)


def approve_review_asset(session: Session, approval: ReviewApproval) -> DriveStatusRow:
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == approval.file_id))
    if asset is None:
        raise DriveFileNotFoundError(f"asset not found: {approval.file_id}")
    if asset.status != "review_required" and not asset.needs_human_review:
        raise ValueError("asset is not review_required")
    original_scope = asset.scope
    record_human_review_override(session, asset, approval)
    asset.scope = original_scope
    asset.status = "move_planned"
    asset.move_status = "planned"
    asset.needs_human_review = False
    asset.review_reason = None
    asset.reviewed_at = datetime.now(UTC)
    asset.reviewed_by = approval.reviewed_by
    rebuild_reviewed_asset_plan(session, asset, apply=True)
    session.commit()
    return drive_status_row(asset)


def record_human_review_override(session: Session, asset: Asset, approval: ReviewApproval) -> None:
    if approval.primary_theme:
        asset.primary_theme = controlled_classification(
            ManagedAIResult(primary_theme=approval.primary_theme, primary_topic=asset.primary_topic or "otros")
        ).primary_theme
    if approval.primary_topic and is_meaningful_naming_text(approval.primary_topic):
        asset.primary_topic = normalize_primary_topic(approval.primary_topic)
    elif not is_meaningful_naming_text(asset.primary_topic):
        asset.primary_topic = None
    if approval.tags:
        update_latest_analysis_tags(session, asset, list(approval.tags))
    latest_analysis = session.scalar(
        select(AssetAIAnalysis)
        .where(AssetAIAnalysis.asset_id == asset.id)
        .order_by(AssetAIAnalysis.created_at.desc(), AssetAIAnalysis.id.desc())
        .limit(1)
    )
    human_result = dict(latest_analysis.result_json) if latest_analysis else {}
    if approval.visual_presentation is not None:
        human_result["visual_presentation"] = approval.visual_presentation
        human_result["contains_people"] = approval.visual_presentation != "not_applicable"
    if approval.visual_presentation_confidence is not None:
        human_result["visual_presentation_confidence"] = approval.visual_presentation_confidence
    if approval.person_visibility is not None:
        human_result["person_visibility"] = approval.person_visibility
    if approval.people_count is not None:
        human_result["people_count"] = approval.people_count
        human_result["contains_people"] = approval.people_count > 0
    elif approval.visual_presentation == "not_applicable":
        human_result["people_count"] = None
        human_result["contains_people"] = False
    human_result.update(
        {
            "primary_theme": asset.primary_theme,
            "primary_topic": asset.primary_topic,
            "tags": list(approval.tags) if approval.tags else human_result.get("tags", []),
            "human_review_overrides_ai": True,
            "reviewed_by": approval.reviewed_by,
        }
    )
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="human-review",
            provider="human",
            input_type="manual",
            prompt_version="human_review_v1",
            result_json=human_result,
            confidence=approval.visual_presentation_confidence,
        )
    )
    session.flush()


def review_approval_has_meaningful_naming_source(asset: Asset, approval: ReviewApproval) -> bool:
    human_result = latest_human_result_for_review(asset)
    if approval.primary_topic is not None:
        human_result["primary_topic"] = clean_review_value(approval.primary_topic)
    if approval.tags:
        human_result["tags"] = list(approval.tags)
    if approval.visual_presentation is not None:
        human_result["visual_presentation"] = approval.visual_presentation
    if approval.visual_presentation_confidence is not None:
        human_result["visual_presentation_confidence"] = approval.visual_presentation_confidence
    if approval.person_visibility is not None:
        human_result["person_visibility"] = approval.person_visibility
    if approval.people_count is not None:
        human_result["people_count"] = approval.people_count
    candidate_asset = asset
    if approval.primary_topic is not None:
        candidate_asset = replace_asset_naming_topic(asset, approval.primary_topic)
    return reviewed_asset_naming_text(candidate_asset, human_result, latest_result_for_review(asset)) is not None


def latest_human_result_for_review(asset: Asset) -> dict[str, Any]:
    analyses = sorted(asset.ai_analyses, key=lambda item: item.created_at or datetime.min, reverse=True)
    for analysis in analyses:
        if analysis.provider == "human" or analysis.model == "human-review":
            return dict(analysis.result_json or {})
    return {}


def latest_result_for_review(asset: Asset) -> dict[str, Any]:
    analyses = sorted(asset.ai_analyses, key=lambda item: item.created_at or datetime.min, reverse=True)
    return dict(analyses[0].result_json or {}) if analyses else {}


def clean_review_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def replace_asset_naming_topic(asset: Asset, primary_topic: str | None) -> Any:
    class NamingAsset:
        pass

    candidate = NamingAsset()
    candidate.primary_topic = clean_review_value(primary_topic) or asset.primary_topic
    candidate.target_name = asset.target_name
    candidate.filename = asset.filename
    candidate.title = asset.title
    candidate.action_description = asset.action_description
    candidate.visual_description = asset.visual_description
    candidate.description = asset.description
    return candidate


def update_reviewed_asset_metadata(session: Session, asset: Asset, approval: ReviewApproval) -> ReviewedReplanResult | None:
    record_human_review_override(session, asset, approval)
    asset.reviewed_at = datetime.now(UTC)
    asset.reviewed_by = approval.reviewed_by
    if asset.move_status == "planned":
        result = rebuild_reviewed_asset_plan(session, asset, apply=True, preserve_batch=True)
        session.commit()
        return result
    session.commit()
    if asset.move_status == "moved":
        result = rebuild_reviewed_asset_plan(session, asset, apply=False, preserve_batch=True)
        session.rollback()
        return result
    return None


def reviewed_asset_classification(asset: Asset) -> Classification:
    normalized = controlled_classification(
        ManagedAIResult(
            primary_theme=asset.primary_theme or "otros",
            primary_topic=asset.primary_topic or "otros",
            confidence={"overall": 1.0},
        )
    )
    return Classification(normalized.primary_theme, normalized.primary_topic, ambiguous=False)


def reviewed_asset_naming_result(asset: Asset) -> ManagedAIResult:
    analyses = sorted(asset.ai_analyses, key=lambda item: item.created_at or datetime.min, reverse=True)
    human_result = next(
        (
            dict(analysis.result_json or {})
            for analysis in analyses
            if analysis.provider == "human" or analysis.model == "human-review"
        ),
        {},
    )
    latest_result = dict(analyses[0].result_json or {}) if analyses else {}
    naming_text = reviewed_asset_naming_text(asset, human_result, latest_result)
    if naming_text is None:
        return ManagedAIResult(
            primary_theme=asset.primary_theme or latest_result.get("primary_theme"),
            primary_topic=asset.primary_topic or latest_result.get("primary_topic"),
            visual_presentation=latest_result.get("visual_presentation") or "not_applicable",
            visual_presentation_confidence=float(latest_result.get("visual_presentation_confidence") or 0.0),
            person_visibility=latest_result.get("person_visibility") or "not_applicable",
            people_count=latest_result.get("people_count"),
            has_visible_text=bool(asset.has_visible_text),
            has_logo=bool(asset.has_logo),
            generic_compatibility=bool(asset.generic_compatibility),
            camera_motion=asset.camera_motion or "unknown",
            shot_type="unknown",
            subject_position=asset.subject_position or "unknown",
            confidence={"overall": 1.0},
            ai_analysis_mode="PERSISTED",
        )
    return ManagedAIResult(
        title_es=naming_text,
        description_es=asset.description or latest_result.get("description_es"),
        primary_theme=asset.primary_theme or latest_result.get("primary_theme"),
        primary_topic=asset.primary_topic or latest_result.get("primary_topic"),
        tags=[str(tag) for tag in (human_result.get("tags") or latest_result.get("tags") or [])],
        visual_presentation=latest_result.get("visual_presentation") or "not_applicable",
        visual_presentation_confidence=float(latest_result.get("visual_presentation_confidence") or 0.0),
        person_visibility=latest_result.get("person_visibility") or "not_applicable",
        people_count=latest_result.get("people_count"),
        has_visible_text=bool(asset.has_visible_text),
        has_logo=bool(asset.has_logo),
        generic_compatibility=bool(asset.generic_compatibility),
        camera_motion=asset.camera_motion or "unknown",
        shot_type="unknown",
        subject_position=asset.subject_position or "unknown",
        confidence={"overall": 1.0},
        ai_analysis_mode="PERSISTED",
    )


PLACEHOLDER_NAMING_SLUGS = {
    "revision",
    "revision-manual",
    "manual-review",
    "ai-response-invalid",
    "respuesta-nvidia-invalida",
    "otros",
    "otro",
    "asset",
    "archivo",
    "video",
    "imagen",
}
PLACEHOLDER_NAMING_PREFIXES = (
    "revision-manual",
    "manual-review",
    "ai-response-invalid",
    "respuesta-nvidia-invalida",
    "respuesta-nvidia-invalida-para",
)
NON_SEMANTIC_NAMING_PREFIXES = (
    "someone-is-",
    "there-are-",
    "there-is-",
    "a-group-of-",
)
SPANISH_NAMING_TERMS = {
    "automovil",
    "auto",
    "calle",
    "celular",
    "chofer",
    "coche",
    "cocina",
    "comida",
    "conduccion",
    "conduciendo",
    "conductor",
    "desde",
    "familia",
    "globos",
    "gps",
    "interior",
    "maneja",
    "manejando",
    "mensajes",
    "mientras",
    "mujer",
    "noche",
    "nocturna",
    "nocturno",
    "personas",
    "telefono",
    "tunel",
    "usando",
    "vista",
    "cama",
    "cumpleanos",
    "expresion",
    "preocupada",
}


def reviewed_asset_naming_text(
    asset: Any,
    human_result: dict[str, Any],
    latest_result: dict[str, Any],
) -> str | None:
    current = existing_semantic_stem(asset.target_name or asset.filename)
    candidates: list[tuple[str, str | None]] = []
    candidates.extend(("topic", value) for value in [asset.primary_topic, human_result.get("primary_topic")])
    candidates.extend(("human", value) for value in human_naming_values(human_result))
    candidates.extend(
        ("asset", value)
        for value in [asset.title, asset.action_description, asset.visual_description, asset.description]
    )
    candidates.extend(("analysis", value) for value in analysis_naming_values(latest_result))
    if is_meaningful_naming_text(current):
        candidates.append(("existing", current))

    for source, value in candidates:
        if not is_meaningful_naming_text(value):
            continue
        candidate = str(value).strip()
        if (
            source != "existing"
            and is_descriptive_spanish_stem(current)
            and candidate_is_less_descriptive(source, candidate, current)
        ):
            return current
        return normalize_naming_text(candidate)
    return None


def human_naming_values(result: dict[str, Any]) -> list[str]:
    return [
        str(value)
        for value in [
            result.get("subject"),
            result.get("action"),
            result.get("context"),
            result.get("title_es"),
            result.get("description_es"),
            result.get("visual_description"),
            result.get("action_description"),
        ]
        if value
    ]


def analysis_naming_values(result: dict[str, Any]) -> list[str]:
    return [
        str(value)
        for value in [
            result.get("title_es"),
            result.get("description_es"),
            result.get("subject"),
            result.get("action"),
            result.get("context"),
            result.get("visual_description"),
            result.get("action_description"),
        ]
        if value
    ]

def is_meaningful_naming_text(value: str | None, *, allow_short: bool = False) -> bool:
    raw = (value or "").strip()
    if PurePosixPath(raw).suffix.lower() in {".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp"}:
        return False
    slug = clean_semantic_slug(value)
    if not slug:
        return False
    if slug in PLACEHOLDER_NAMING_SLUGS:
        return False
    if any(slug.startswith(prefix) for prefix in PLACEHOLDER_NAMING_PREFIXES):
        return False
    if any(slug.startswith(prefix) for prefix in NON_SEMANTIC_NAMING_PREFIXES):
        return False
    words = slug.split("-")
    if len(words) < (1 if allow_short else 2):
        return False
    if len(words) == 1 and words[0] in TAG_GENERIC_TERMS:
        return False
    return True


def existing_semantic_stem(filename: str | None) -> str | None:
    if not filename:
        return None
    stem = PurePosixPath(filename).name
    stem = re.sub(r"__[a-z0-9]+__[a-f0-9]{8}\.[a-z0-9]+$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", stem, flags=re.IGNORECASE)
    return stem or None


def is_descriptive_spanish_stem(value: str | None) -> bool:
    slug = clean_semantic_slug(value)
    words = slug.split("-")
    if len(words) < 4:
        return False
    return any(word in SPANISH_NAMING_TERMS for word in words) or bool(re.search(r"[áéíóúüñ]", value or "", re.I))


def candidate_is_less_descriptive(source: str, candidate: str, current: str | None) -> bool:
    if source not in {"topic", "analysis"}:
        return False
    candidate_words = clean_semantic_slug(candidate).split("-")
    current_words = clean_semantic_slug(current).split("-")
    return len(candidate_words) <= 3 and len(current_words) >= len(candidate_words) + 3


def normalize_naming_text(value: str) -> str:
    return re.sub(r"\bmanjeando\b", "manejando", value, flags=re.IGNORECASE)


def rebuild_reviewed_asset_plan(
    session: Session,
    asset: Asset,
    *,
    apply: bool,
    preserve_batch: bool = False,
) -> ReviewedReplanResult:
    before = ReviewedReplanResult(
        dry_run=not apply,
        file_id=asset.drive_file_id or "",
        changed=False,
        skipped_reason=None,
        target_name_before=asset.target_name,
        target_name_after=None,
        target_path_before=asset.remote_path,
        target_path_after=None,
        plan_hash_before=asset.plan_hash,
        plan_hash_after=None,
        status_before=asset.status,
        status_after=None,
        move_status_before=asset.move_status,
        move_status_after=None,
    )
    asset.primary_theme = asset.primary_theme or "otros"
    asset.primary_topic = asset.primary_topic or "otros"
    request_data = ingest_request_from_asset(asset)
    classification = reviewed_asset_classification(asset)
    stored_batch = stored_batch_number(asset)
    batch_number = stored_batch if preserve_batch and stored_batch is not None else next_batch_number(
        session,
        request_data,
        asset,
        classification,
        requires_review=False,
        exclude_asset_id=asset.id,
    )
    target_parts = route_parts(
        request_data,
        asset.brand,
        asset.type or "video",
        classification,
        asset.orientation,
        batch_number,
        requires_review=False,
        asset=asset,
    )
    naming_result = reviewed_asset_naming_result(asset)
    if not semantic_filename_parts(naming_result):
        return replace(before, skipped_reason="no_meaningful_naming_source")
    target_name = target_filename(request_data, asset.brand, asset, naming_result)
    if not valid_target_filename(target_name):
        return replace(before, skipped_reason="no_meaningful_naming_source")
    target_path = str(PurePosixPath(*target_parts) / target_name)
    status_after = "move_planned"
    move_status_after = "planned"
    old_path_layout_version = asset.path_layout_version
    asset.path_layout_version = PATH_LAYOUT_VERSION
    plan_hash = compute_plan_hash(asset, target_path, target_name, batch_number)
    asset.path_layout_version = old_path_layout_version
    changed = (
        asset.target_name != target_name
        or asset.remote_path != target_path
        or asset.plan_hash != plan_hash
        or asset.status != status_after
        or asset.move_status != move_status_after
        or asset.path_layout_version != PATH_LAYOUT_VERSION
    )
    result = replace(
        before,
        changed=changed,
        target_name_after=target_name,
        target_path_after=target_path,
        plan_hash_after=plan_hash,
        status_after=status_after,
        move_status_after=move_status_after,
    )
    if not apply:
        return result
    asset.remote_path = target_path
    asset.source_path = target_path
    asset.target_name = target_name
    asset.status = status_after
    asset.move_status = move_status_after
    asset.needs_human_review = False
    asset.review_reason = None
    asset.path_layout_version = PATH_LAYOUT_VERSION
    asset.plan_version = PLAN_VERSION
    asset.plan_created_at = datetime.now(UTC)
    asset.plan_hash = plan_hash
    return result


def replan_reviewed_assets(
    session: Session,
    *,
    apply: bool = False,
    only_file_id: str | None = None,
    limit: int = 100,
) -> list[ReviewedReplanResult]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    query = (
        select(Asset)
        .where(
            Asset.drive_file_id.is_not(None),
            Asset.status == "move_planned",
            Asset.move_status == "planned",
            Asset.reviewed_at.is_not(None),
        )
        .order_by(Asset.reviewed_at.asc(), Asset.id.asc())
        .limit(limit)
    )
    if only_file_id:
        query = query.where(Asset.drive_file_id == only_file_id)
    results = [
        rebuild_reviewed_asset_plan(session, asset, apply=apply, preserve_batch=True)
        for asset in session.scalars(query).all()
    ]
    if apply:
        session.commit()
    else:
        session.rollback()
    return results


def rename_reviewed_moved_asset(
    session: Session,
    drive_client: DriveClient | None,
    *,
    file_id: str,
    apply: bool = False,
) -> MovedRenameResult:
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == file_id))
    if asset is None:
        raise DriveFileNotFoundError(f"asset not found: {file_id}")
    if asset.move_status != "moved" or asset.status not in {"ready", "review_required"}:
        raise ValueError("asset is not a moved Drive asset")
    if apply and drive_client is None:
        raise ValueError("drive_client is required when apply=True")
    metadata = drive_client.get_file_metadata(file_id) if apply and drive_client is not None else None
    parent_before = (
        metadata.parents[0]
        if metadata is not None and metadata.parents
        else asset.target_parent_id or asset.original_parent_id
    )
    plan = rebuild_reviewed_asset_plan(session, asset, apply=False, preserve_batch=True)
    session.rollback()
    if plan.skipped_reason:
        return MovedRenameResult(
            dry_run=not apply,
            file_id=file_id,
            changed=False,
            skipped_reason=plan.skipped_reason,
            drive_file_id_before=metadata.id if metadata is not None else asset.drive_file_id,
            drive_file_id_after=metadata.id if metadata is not None else asset.drive_file_id,
            parent_before=parent_before,
            parent_after=parent_before,
            target_name_before=asset.target_name or (metadata.name if metadata is not None else asset.filename),
            target_name_after=None,
            target_path_before=asset.remote_path,
            target_path_after=None,
            plan_hash_before=asset.plan_hash,
            plan_hash_after=None,
        )
    target_path_after = plan.target_path_after or asset.remote_path
    target_name_after = plan.target_name_after or asset.target_name or asset.filename
    parent_path_before = str(PurePosixPath(asset.remote_path or "").parent)
    parent_path_after = str(PurePosixPath(target_path_after).parent)
    skipped_reason = None
    if parent_path_before != parent_path_after:
        skipped_reason = "target_parent_path_changed"
    current_name = metadata.name if metadata is not None else asset.target_name or asset.filename
    if current_name == target_name_after and not skipped_reason:
        skipped_reason = "already_named"
    result = MovedRenameResult(
        dry_run=not apply,
        file_id=file_id,
        changed=skipped_reason is None,
        skipped_reason=skipped_reason,
        drive_file_id_before=metadata.id if metadata is not None else asset.drive_file_id,
        drive_file_id_after=metadata.id if metadata is not None else asset.drive_file_id,
        parent_before=parent_before,
        parent_after=parent_before,
        target_name_before=asset.target_name or current_name,
        target_name_after=target_name_after,
        target_path_before=asset.remote_path,
        target_path_after=target_path_after,
        plan_hash_before=asset.plan_hash,
        plan_hash_after=plan.plan_hash_after,
    )
    if not apply or skipped_reason is not None:
        return result
    assert drive_client is not None
    updated = drive_client.rename_and_move_file(file_id, target_name_after, parent_before or "", None)
    verified = drive_client.verify_file_location(file_id, target_name_after, parent_before or "")
    if not verified:
        raise MoveError("Drive rename verification failed")
    asset.filename = updated.name
    asset.target_name = target_name_after
    asset.remote_path = target_path_after
    asset.source_path = target_path_after
    asset.target_parent_id = parent_before
    asset.path_layout_version = PATH_LAYOUT_VERSION
    asset.plan_version = PLAN_VERSION
    asset.plan_created_at = datetime.now(UTC)
    asset.plan_hash = plan.plan_hash_after
    asset.status = "ready"
    asset.move_status = "moved"
    session.commit()
    return result


def update_latest_analysis_tags(session: Session, asset: Asset, tags: list[str]) -> None:
    analysis = next(
        iter(sorted(asset.ai_analyses, key=lambda item: item.created_at or datetime.min, reverse=True)),
        None,
    )
    if analysis is None:
        return
    result_json = dict(analysis.result_json or {})
    result_json["tags"] = unique_tags(tags)
    result_json["tags_source"] = "manual_review"
    analysis.result_json = result_json
    session.flush()


def ingest_request_from_asset(asset: Asset) -> IngestRequest:
    settings = get_settings()
    root_id = settings.google_drive_root_folder_id or ""
    source_folder_id = asset.original_parent_id or ""
    return IngestRequest(
        source_folder_id=source_folder_id,
        destination_root_id=root_id,
        scope=asset.scope or "generic",
        limit=1,
        file_id=asset.drive_file_id,
        apply=False,
        dry_run=True,
        brand_slug=asset.brand.slug if asset.brand else None,
        collection=asset.collection or "evergreen",
        title_type=asset.title_type,
        title=asset.title_name,
        season=asset.season_number,
        episode=asset.episode_number,
    )


def apply_candidates(
    session: Session,
    request_data: BatchRequest,
    scopes: list[BatchScope],
) -> list[tuple[Asset | None, BatchScope]]:
    scope_by_name = {scope.scope: scope for scope in scopes}
    if request_data.only_file_ids:
        candidates: list[tuple[Asset, BatchScope]] = []
        if len(request_data.only_file_ids) > request_data.max_total:
            return [
                (
                    session.scalar(select(Asset).where(Asset.drive_file_id == drive_file_id)),
                    BatchScope("", "", ""),
                )
                for drive_file_id in request_data.only_file_ids
            ]
        for drive_file_id in request_data.only_file_ids:
            asset = session.scalar(select(Asset).where(Asset.drive_file_id == drive_file_id))
            scope_config = scope_by_name.get(asset.scope if asset is not None else "")
            if asset is None or scope_config is None:
                candidates.append((asset, BatchScope("", "", "")))
            else:
                candidates.append((asset, scope_config))
        return candidates

    candidates: list[tuple[Asset, BatchScope]] = []
    per_scope: dict[str, int] = {}
    query = (
        select(Asset)
        .where(
            Asset.drive_file_id.is_not(None),
            Asset.status == "move_planned",
            Asset.move_status == "planned",
            Asset.plan_hash.is_not(None),
            Asset.target_name.is_not(None),
            Asset.remote_path.is_not(None),
        )
        .order_by(Asset.plan_created_at.asc().nulls_last(), Asset.created_at.asc(), Asset.id.asc())
    )
    for asset in session.scalars(query).all():
        if len(candidates) >= request_data.max_total:
            break
        scope_config = scope_by_name.get(asset.scope or "")
        if scope_config is None:
            continue
        if per_scope.get(scope_config.scope, 0) >= request_data.max_per_scope:
            continue
        if apply_asset_is_planned(asset):
            candidates.append((asset, scope_config))
            per_scope[scope_config.scope] = per_scope.get(scope_config.scope, 0) + 1
    return candidates


def validate_apply_candidates(request_data: BatchRequest, candidates: list[tuple[Asset | None, BatchScope]]) -> None:
    if request_data.only_file_ids:
        if len(request_data.only_file_ids) > request_data.max_total:
            raise ValueError("--only-file-id count exceeds --max-total")
        seen = set()
        for drive_file_id in request_data.only_file_ids:
            if drive_file_id in seen:
                raise ValueError(f"duplicate --only-file-id: {drive_file_id}")
            seen.add(drive_file_id)
        for drive_file_id, (asset, _scope_config) in zip(request_data.only_file_ids, candidates, strict=True):
            if asset is None:
                raise ValueError(f"--only-file-id is not registered in PostgreSQL: {drive_file_id}")
            if not asset.plan_hash:
                raise ValueError(f"--only-file-id has null plan_hash: {drive_file_id}")
            if not (apply_asset_is_planned(asset) or moved_asset_can_noop(asset)):
                raise ValueError(f"--only-file-id is not apply-eligible: {drive_file_id}")


def apply_asset_is_planned(asset: Asset) -> bool:
    return asset.status == "move_planned" and asset.move_status == "planned" and bool(asset.plan_hash)


def moved_asset_can_noop(asset: Asset) -> bool:
    return asset.status in {"ready", "review_required"} and asset.move_status == "moved" and bool(asset.plan_hash)


def forbidden_batch_ai(*_args: Any, **_kwargs: Any) -> ManagedAIResult:
    raise AssertionError("AI must not be called during batch apply")


def batch_ingest_request(scope_config: BatchScope, file_id: str, apply: bool) -> IngestRequest:
    return IngestRequest(
        source_folder_id=scope_config.source_folder_id,
        destination_root_id=scope_config.destination_root_id,
        scope=scope_config.scope,
        limit=1,
        file_id=file_id,
        apply=apply,
        dry_run=not apply,
        brand_slug=scope_config.brand_slug,
        collection=scope_config.collection,
        title_type=scope_config.title_type,
        title=scope_config.title,
        season=scope_config.season,
        episode=scope_config.episode,
    )


def acquire_batch_lock(session: Session) -> bool:
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        return bool(session.execute(text("SELECT pg_try_advisory_lock(488401270812)")).scalar())
    if dialect == "sqlite":
        try:
            session.execute(text("BEGIN IMMEDIATE"))
        except Exception:
            session.rollback()
            return False
        return True
    return True


def release_batch_lock(session: Session) -> None:
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    try:
        if dialect == "postgresql":
            session.execute(text("SELECT pg_advisory_unlock(488401270812)"))
            session.commit()
        elif dialect == "sqlite":
            session.commit()
    except Exception:
        session.rollback()


def skipped_batch_asset(drive_file: DriveFile, scope_config: BatchScope, reason: str) -> BatchAssetResult:
    return BatchAssetResult(
        drive_file_id=drive_file.id,
        scope=scope_config.scope,
        original_name=drive_file.name,
        analysis_result="skipped",
        final_status="skipped",
        move_status="skipped",
        error=reason,
    )


def skipped_existing_asset(asset: Asset, drive_file: DriveFile, scope_config: BatchScope) -> BatchAssetResult:
    return BatchAssetResult(
        drive_file_id=drive_file.id,
        scope=asset.scope or scope_config.scope,
        original_name=asset.original_name or drive_file.name,
        provider=asset.ai_model and "persisted",
        analysis_result="already_registered",
        target_path=asset.remote_path,
        final_status=asset.status,
        move_status=asset.move_status,
        review_reason=asset.review_reason,
    )


def pending_analysis_batch_asset(drive_file: DriveFile, scope_config: BatchScope) -> BatchAssetResult:
    return BatchAssetResult(
        drive_file_id=drive_file.id,
        scope=scope_config.scope,
        original_name=drive_file.name,
        analysis_result="pending_analysis",
        final_status="pending_analysis",
        move_status="not_registered",
        pending_analysis=True,
    )


def batch_asset_from_plan(plan: IngestPlan, session: Session) -> BatchAssetResult:
    asset = session.get(Asset, plan.asset_id) if plan.asset_id else None
    return BatchAssetResult(
        drive_file_id=plan.drive_file_id,
        scope=plan.scope,
        original_name=plan.original_name,
        provider=plan.ai_provider or ("nvidia" if plan.ai_call else "persisted"),
        analysis_result=plan.result,
        target_path=plan.target_path,
        final_status=plan.status,
        move_status=plan.move_status,
        review_reason=(asset.review_reason if asset else None) or review_reason_from_warnings(plan.warnings),
        drive_mutations=plan.drive_mutations_performed,
        error="; ".join(plan.warnings) if plan.status == "failed" else None,
    )


def nvidia_error_is_retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(code in message for code in ("429", "500", "503", "timeout", "timed out"))


def nvidia_error_is_invalid_json(exc: Exception) -> bool:
    return "invalid json" in str(exc).lower()


def drive_client_from_environment() -> DriveClient:
    settings = get_settings()
    client_name = settings.google_drive_client.strip().lower()
    if client_name == "rclone":
        return RcloneDriveClient.from_environment()
    if client_name == "api":
        return GoogleDriveAPIClient.from_environment()
    raise DriveAuthError("GOOGLE_DRIVE_CLIENT must be rclone or api")


def mutation_client_for_apply(read_client: DriveClient) -> DriveClient:
    if isinstance(read_client, RcloneDriveClient):
        return GoogleDriveMutationClient.from_rclone_remote(read_client.remote, read_client._source_config_path)
    return read_client


def request_file_metadata(drive_client: DriveClient, request_data: IngestRequest) -> DriveFile:
    try:
        drive_file = drive_client.get_file_metadata(request_data.file_id or "")
        if not (drive_file.is_folder and not drive_file.name):
            return drive_file
    except DriveFileNotFoundError:
        pass
    for drive_file in drive_client.list_folder(request_data.source_folder_id, max(request_data.limit, 100)):
        if drive_file.id == request_data.file_id:
            return drive_file
    raise DriveFileNotFoundError(f"file not found in source folder: {request_data.file_id}")


def validate_pilot_folders(drive_client: DriveClient, request_data: IngestRequest) -> None:
    if not drive_client.folder_exists(request_data.source_folder_id):
        raise DriveFileNotFoundError("source folder not found or is not a folder")
    if not drive_client.folder_exists(request_data.destination_root_id):
        raise DriveFileNotFoundError("destination root not found or is not a folder")


def process_drive_file(
    session: Session,
    request_data: IngestRequest,
    drive_client: DriveClient,
    mutation_client: DriveClient,
    drive_file: DriveFile,
    technical_analyzer: Any,
    preview_generator: Any,
    ai_enricher: Any,
) -> IngestPlan:
    source = get_or_create_pilot_source(session)
    existing = session.scalar(select(Asset).where(Asset.drive_file_id == drive_file.id))
    if existing and existing.status == "ready":
        if asset_matches_drive_file(existing, drive_file):
            return plan_from_ready_asset(existing, request_data)
    if (
        existing
        and not request_data.replan
        and existing.status in {"move_planned", "review_required"}
        and existing.move_status == "planned"
        and existing.target_name
        and existing.remote_path
    ):
        if existing.path_layout_version != PATH_LAYOUT_VERSION:
            replan_persisted_asset_compact_v2(session, request_data, existing)
        if existing.status == "review_required" and not existing.review_reason:
            existing.review_reason = review_reason_for_asset(existing)
            session.commit()
        plan = plan_from_persisted_asset(existing, request_data, drive_file)
        assert_persisted_plan_hash(existing)
        if request_data.dry_run:
            return plan
        target_parts = str(existing.remote_path).split("/")[:-1]
        return apply_move(session, mutation_client, existing, request_data, target_parts, plan)

    brand = resolve_brand(session, request_data) if request_data.scope == "brand" else None
    original_parent_id = drive_file.parents[0] if drive_file.parents else request_data.source_folder_id
    asset = existing or Asset(
        asset_uid=f"drive-{short_id(drive_file.id)}",
        source=source,
        provider="google_drive",
        rclone_remote=None,
        remote_path=str(PurePosixPath(original_parent_id) / drive_file.name),
        source_path=str(PurePosixPath(original_parent_id) / drive_file.name),
        drive_file_id=drive_file.id,
        remote_file_id=drive_file.id,
        filename=drive_file.name,
    )
    session.add(asset)
    apply_scope_fields(asset, request_data, brand)
    asset.status = "downloading"
    asset.source_status = "active"
    asset.original_parent_id = asset.original_parent_id or original_parent_id
    asset.original_name = asset.original_name or drive_file.name
    asset.source_size_bytes = drive_file.size
    asset.source_modified_at = drive_file.modified_time
    session.flush()

    with tempfile.TemporaryDirectory(prefix="kurukin-drive-pilot-") as temp_name:
        temp_dir = Path(temp_name)
        local_file = temp_dir / safe_original_filename(drive_file.name)
        try:
            drive_client.download_file(drive_file.id, local_file)
        except Exception as exc:
            raise typed_error("download_error", exc) from exc

        checksum = sha256_file(local_file)
        asset.checksum = checksum
        asset.source_hash = drive_file.md5_checksum or checksum
        asset.status = "technical_analysis"
        session.flush()

        technical: TechnicalResult = technical_analyzer(local_file, drive_file)
        asset.mime_type = technical.mime_type
        asset.type = technical.media_type
        asset.width = technical.width
        asset.height = technical.height
        asset.duration_seconds = technical.duration_seconds
        asset.fps = technical.fps
        asset.codec = technical.codec
        asset.has_audio = technical.has_audio
        asset.orientation = pilot_orientation(technical.width, technical.height)

        asset.status = "preview_generation"
        session.flush()
        previews: PreviewResult = preview_generator(asset, local_file, technical)
        asset.thumbnail_path = previews.thumbnail_path
        asset.preview_path = previews.preview_path
        asset.preview_status = "ready" if previews.thumbnail_path or previews.preview_path else "skipped"
        asset.technical_metadata_status = "ready"
        asset.preview_generated_at = datetime.now(UTC)
        thumbnail_size = pilot_preview_size(previews.thumbnail_path)
        thumbnail_valid = pilot_preview_is_valid(previews.thumbnail_path)

        asset.status = "ai_analysis"
        session.flush()
        ai_result: ManagedAIResult = ai_enricher(asset, local_file, previews)
        ai_result = normalize_managed_people_metadata(ai_result, drive_file.name)
        classification = controlled_classification(ai_result)
        warnings = list(previews.warnings)
        warnings.extend(ai_result.warnings)
        ai_result, tag_warnings = ensure_managed_tags(ai_result, classification)
        warnings.extend(tag_warnings)
        insufficient_ai = ai_classification_insufficient(ai_result, classification)
        if ai_result.ai_analysis_mode == "FALLBACK":
            warnings.append("ai_analysis_fallback")
        if insufficient_ai:
            warnings.append("ai_classification_insufficient")
        if classification.ambiguous:
            warnings.append("classification_ambiguous")
        if ai_result.requires_review and "ai_requires_review" not in warnings:
            warnings.append("ai_requires_review")
        forced_review = classification.ambiguous or insufficient_ai or ai_result.requires_review
        review_reason = review_reason_from_warnings(warnings, ai_result, classification, insufficient_ai)

        asset.primary_theme = classification.primary_theme
        asset.primary_topic = classification.primary_topic
        asset.description = text_or_none(ai_result.description_es)
        asset.search_text = " ".join(ai_result.search_terms).strip() or None
        asset.suggested_uses = sanitized_list(ai_result.suggested_uses, 12)
        asset.flip_horizontal_allowed = ai_result.can_flip_horizontal
        asset.flip_risk_reasons = sanitized_list(ai_result.flip_risk_reasons, 8)
        asset.zoom_allowed = ai_result.can_zoom
        asset.max_safe_zoom = max(1.0, min(float(ai_result.max_safe_zoom or 1.0), 3.0))
        asset.has_visible_text = ai_result.has_visible_text
        asset.has_logo = ai_result.has_logo
        asset.generic_compatibility = bool(ai_result.generic_compatibility)
        asset.camera_motion = normalize_existing_enum(ai_result.camera_motion)
        asset.shot_type = normalize_existing_enum(ai_result.shot_type)
        asset.subject_position = normalize_existing_enum(ai_result.subject_position)
        asset.safe_text_areas = sanitized_list(ai_result.safe_text_areas, 8)
        asset.ai_enrichment_status = "needs_review" if forced_review else "ready"
        asset.ai_enrichment_confidence = min(ai_confidence(ai_result), 1.0)
        asset.ai_model = ai_result.ai_model
        asset.ai_enriched_at = datetime.now(UTC)
        asset.needs_human_review = forced_review
        asset.review_reason = review_reason if asset.needs_human_review else None
        asset.catalog_version = CATALOG_VERSION
        asset.usage_policy = usage_policy_for_request(request_data, ai_result)
        persist_ai_analysis(session, asset, ai_result)

        batch_number = next_batch_number(session, request_data, asset, classification, requires_review=forced_review)
        target_parts = route_parts(
            request_data,
            brand,
            technical.media_type,
            classification,
            asset.orientation,
            batch_number,
            requires_review=forced_review,
            asset=asset,
        )
        target_name = target_filename(request_data, brand, asset, ai_result)
        target_path = str(PurePosixPath(*target_parts) / target_name)
        asset.target_name = target_name
        asset.remote_path = target_path
        asset.source_path = target_path
        asset.status = "review_required" if forced_review else "move_planned"
        asset.review_reason = review_reason if asset.status == "review_required" else None
        asset.move_status = "planned"
        asset.path_layout_version = PATH_LAYOUT_VERSION
        asset.plan_version = PLAN_VERSION
        asset.plan_created_at = asset.plan_created_at or datetime.now(UTC)
        asset.plan_hash = compute_plan_hash(asset, target_path, target_name, batch_number)
        session.commit()

        plan = IngestPlan(
            drive_file_id=drive_file.id,
            original_name=drive_file.name,
            original_parent_id=original_parent_id,
            source_drive_id=drive_file.drive_id,
            scope=request_data.scope,
            brand=brand.slug if brand else None,
            title=request_data.title,
            collection=request_data.collection if request_data.scope == "brand" else None,
            mime_type=technical.mime_type,
            media_type=technical.media_type,
            width=technical.width,
            height=technical.height,
            duration=technical.duration_seconds,
            aspect_ratio=aspect_ratio(technical.width, technical.height),
            orientation=asset.orientation,
            title_es=ai_result.title_es,
            description_es=ai_result.description_es,
            primary_theme=classification.primary_theme,
            primary_topic=classification.primary_topic,
            suggested_tags=sanitized_list(ai_result.tags, 20),
            tags_source=ai_result.tags_source,
            suggested_uses=sanitized_list(ai_result.suggested_uses, 12),
            can_flip_horizontal=ai_result.can_flip_horizontal,
            flip_risk_reasons=sanitized_list(ai_result.flip_risk_reasons, 8),
            can_zoom=ai_result.can_zoom,
            max_safe_zoom=asset.max_safe_zoom,
            has_visible_text=asset.has_visible_text,
            has_logo=asset.has_logo,
            generic_compatibility=asset.generic_compatibility,
            target_path=target_path,
            target_name=target_name,
            target_batch=batch_number,
            path_layout_version=PATH_LAYOUT_VERSION,
            preview_path=previews.preview_path,
            thumbnail_path=previews.thumbnail_path,
            classification_confidence=ai_confidence(ai_result),
            warnings=warnings,
            requires_review=forced_review,
            database_status=asset.status,
            asset_id=asset.id,
            status=asset.status,
            move_status=asset.move_status,
            ai_analysis_mode=ai_result.ai_analysis_mode,
            ai_provider=ai_result.ai_provider,
            ai_model=ai_result.ai_model,
            frames_analyzed=ai_result.frames_analyzed,
            thumbnail_valid=thumbnail_valid,
            thumbnail_size=thumbnail_size,
            plan_version=asset.plan_version,
            plan_created_at=asset.plan_created_at,
            plan_hash=asset.plan_hash,
            ai_call=True,
        )
        if request_data.dry_run:
            return plan

        return apply_move(session, mutation_client, asset, request_data, target_parts, plan)


def apply_move(
    session: Session,
    drive_client: DriveClient,
    asset: Asset,
    request_data: IngestRequest,
    target_parts: list[str],
    plan: IngestPlan,
) -> IngestPlan:
    stats = MutationStats()
    asset.status = "moving"
    asset.move_status = "moving"
    session.commit()
    try:
        assert_persisted_plan_hash(asset)
        target_parent_id = ensure_managed_folder(
            session,
            drive_client,
            request_data,
            asset,
            target_parts,
            stats,
            create_missing=not request_data.simulate_drive_mutation,
        )
        asset.target_parent_id = target_parent_id
        session.commit()
        original_parent_id = asset.original_parent_id or plan.original_parent_id or ""
        original_name = asset.original_name or plan.original_name
        if request_data.simulate_drive_mutation:
            asset.status = "move_planned"
            asset.move_status = "planned"
            session.commit()
            simulated_parent = str(PurePosixPath(request_data.destination_root_id, *target_parts))
            return replace(
                plan,
                status="move_planned",
                move_status="planned",
                mutation_stats=stats.to_dict(),
                result="simulated",
                simulated_update=build_update_simulation(
                    asset.drive_file_id or "",
                    original_parent_id,
                    simulated_parent,
                    original_name,
                    asset.target_name or asset.filename,
                    asset.plan_hash or plan.plan_hash,
                    would_create_folders=target_parts,
                ),
            )
        before = drive_client.get_file_metadata(asset.drive_file_id or "")
        validate_move_preconditions(before, original_parent_id, target_parent_id, drive_client)
        stats.attempt("file_update_attempted")
        drive_client.rename_and_move_file(
            asset.drive_file_id or "",
            asset.target_name or asset.filename,
            target_parent_id,
            original_parent_id,
        )
        stats.succeed("file_update_succeeded")
        if not verified_moved_file(
            drive_client,
            asset.drive_file_id or "",
            asset.target_name or "",
            target_parent_id,
            original_parent_id,
            before,
        ):
            raise typed_error("verification_error", "Drive verification failed")
        folder = session.scalar(
            select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == target_parent_id)
        )
        if folder is not None:
            folder.item_count += 1
        asset.filename = asset.target_name or asset.filename
        asset.remote_path = plan.target_path
        asset.source_path = plan.target_path
        asset.moved_at = datetime.now(UTC)
        asset.move_status = "moved"
        final_status = "review_required" if plan.requires_review or asset.needs_human_review else "ready"
        asset.status = final_status
        if final_status == "review_required" and not asset.review_reason:
            asset.review_reason = review_reason_from_warnings(plan.warnings)
        session.commit()
        return replace(
            plan,
            status=final_status,
            move_status="moved",
            drive_mutations_performed=len(stats.succeeded),
            mutation_stats=stats.to_dict(),
            database_status=final_status,
            result="applied",
            ai_call=plan.ai_call,
        )
    except Exception as exc:
        session.rollback()
        failed = session.get(Asset, asset.id)
        if failed is None:
            raise
        try:
            stats.attempt("rollback_attempted")
            drive_client.restore_file_location(
                failed.drive_file_id or "",
                failed.original_name or failed.filename,
                failed.original_parent_id or "",
            )
            if drive_client.verify_file_location(
                failed.drive_file_id or "",
                failed.original_name or failed.filename,
                failed.original_parent_id or "",
            ):
                stats.succeed("rollback_succeeded")
                stats.compensate("rollback_succeeded")
                failed.move_status = "planned"
                failed.status = "move_planned"
                failed.review_reason = error_type(exc)
            else:
                stats.uncompensate("rollback_incomplete")
                failed.move_status = "rollback_required"
                failed.status = "review_required"
                failed.review_reason = f"{error_type(exc)}; rollback_incomplete"
        except Exception as rollback_exc:
            stats.uncompensate("rollback_failed")
            failed.move_status = "rollback_required"
            failed.status = "review_required"
            failed.review_reason = f"{error_type(exc)}; rollback_error: {sanitize_error_message(str(rollback_exc))}"
        session.commit()
        return replace(
            plan,
            status=failed.status,
            move_status=failed.move_status,
            warnings=plan.warnings + [failed.review_reason or "move_error"],
            drive_mutations_performed=len(stats.succeeded),
            mutation_stats=stats.to_dict(),
            database_status=failed.status,
            result="move_failed",
            ai_call=plan.ai_call,
        )


def ensure_managed_folder(
    session: Session,
    drive_client: DriveClient,
    request_data: IngestRequest,
    asset: Asset,
    target_parts: list[str],
    stats: MutationStats | None = None,
    create_missing: bool = True,
) -> str:
    batch_size = get_settings().managed_folder_batch_size
    batch_number = int(target_parts[-1].removeprefix("lote-"))
    folder = find_managed_folder(session, request_data, asset, batch_number)
    if folder is not None and folder.item_count < batch_size and drive_client.folder_exists(folder.drive_folder_id):
        return folder.drive_folder_id
    if folder is not None and folder.item_count >= batch_size:
        batch_number += 1
        target_parts[-1] = f"lote-{batch_number:04d}"
        folder = find_managed_folder(session, request_data, asset, batch_number)
        if folder is not None and drive_client.folder_exists(folder.drive_folder_id):
            return folder.drive_folder_id
    if not create_missing:
        return str(PurePosixPath(request_data.destination_root_id, *target_parts))

    parent_id = request_data.destination_root_id
    for part in target_parts:
        if stats is not None:
            stats.attempt("folder_created")
        parent_id = drive_client.create_folder(parent_id, part)
        if stats is not None:
            stats.succeed("folder_created")
    folder = ManagedDriveFolder(
        drive_folder_id=parent_id,
        root_folder_id=request_data.destination_root_id,
        scope=request_data.scope,
        brand_id=asset.brand_id,
        title_slug=asset.title_slug,
        title_type=asset.title_type,
        collection=asset.collection,
        media_type=asset.type,
        path_layout_version=PATH_LAYOUT_VERSION,
        context_slug=folder_context_slug(request_data, asset),
        primary_theme=None,
        primary_topic=compact_folder_primary_topic(asset),
        orientation="compact",
        batch_number=batch_number,
    )
    session.add(folder)
    session.flush()
    return parent_id


def find_managed_folder(
    session: Session,
    request_data: IngestRequest,
    asset: Asset,
    batch_number: int,
) -> ManagedDriveFolder | None:
    return session.scalar(
        select(ManagedDriveFolder).where(
            ManagedDriveFolder.root_folder_id == request_data.destination_root_id,
            ManagedDriveFolder.scope == request_data.scope,
            ManagedDriveFolder.brand_id == asset.brand_id,
            ManagedDriveFolder.title_slug == asset.title_slug,
            ManagedDriveFolder.title_type == asset.title_type,
            ManagedDriveFolder.collection == asset.collection,
            ManagedDriveFolder.media_type == asset.type,
            ManagedDriveFolder.path_layout_version == PATH_LAYOUT_VERSION,
            ManagedDriveFolder.context_slug == folder_context_slug(request_data, asset),
            ManagedDriveFolder.primary_theme.is_(None),
            ManagedDriveFolder.primary_topic == compact_folder_primary_topic(asset),
            ManagedDriveFolder.orientation == "compact",
            ManagedDriveFolder.batch_number == batch_number,
        )
    )


def replan_persisted_asset_compact_v2(session: Session, request_data: IngestRequest, asset: Asset) -> None:
    if asset.move_status != "planned" or asset.status not in {"move_planned", "review_required"}:
        return
    classification = Classification(asset.primary_theme or "otros", asset.primary_topic or "otros")
    batch_number = next_batch_number(
        session,
        request_data,
        asset,
        classification,
        requires_review=asset.status == "review_required" or asset.needs_human_review,
        exclude_asset_id=asset.id,
    )
    target_parts = route_parts(
        request_data,
        asset.brand,
        asset.type,
        classification,
        asset.orientation,
        batch_number,
        requires_review=asset.status == "review_required" or asset.needs_human_review,
        asset=asset,
    )
    target_name = asset.target_name or asset.filename
    target_path = str(PurePosixPath(*target_parts) / target_name)
    asset.remote_path = target_path
    asset.source_path = target_path
    asset.path_layout_version = PATH_LAYOUT_VERSION
    asset.plan_version = PLAN_VERSION
    asset.plan_created_at = asset.plan_created_at or datetime.now(UTC)
    asset.plan_hash = compute_plan_hash(asset, target_path, target_name, batch_number)
    session.commit()


def audit_legacy_layout_assets(
    session: Session,
    drive_client: DriveClient,
    root_folder_id: str,
) -> list[LegacyLayoutAssetPlan]:
    assets = session.scalars(
        select(Asset).where(
            Asset.move_status == "moved",
            Asset.drive_file_id.is_not(None),
        )
    ).all()
    plans: list[LegacyLayoutAssetPlan] = []
    for asset in assets:
        if not asset_is_legacy_layout(asset):
            continue
        plans.append(build_legacy_layout_plan(session, drive_client, root_folder_id, asset))
    return plans


def asset_is_legacy_layout(asset: Asset) -> bool:
    if asset.path_layout_version != PATH_LAYOUT_VERSION:
        return True
    path = asset.remote_path or ""
    parts = [part for part in path.split("/") if part]
    return (
        ("horizontal-16x9" in parts or "vertical-9x16" in parts or "vertical-4x5" in parts)
        or len(parts) > len(compact_target_parts_for_asset(asset, stored_batch_number(asset) or 1)) + 1
    )


def build_legacy_layout_plan(
    session: Session,
    drive_client: DriveClient,
    root_folder_id: str,
    asset: Asset,
) -> LegacyLayoutAssetPlan:
    metadata = drive_client.get_file_metadata(asset.drive_file_id or "")
    if not metadata.parents:
        raise LegacyLayoutMigrationError(f"file has no parent: {asset.drive_file_id}")
    batch_number = stored_batch_number(asset) or 1
    target_parts = compact_target_parts_for_asset(asset, batch_number)
    target_path = str(PurePosixPath(*target_parts) / (asset.target_name or metadata.name))
    target_parent_id = existing_compact_folder_id(session, asset, root_folder_id, batch_number)
    current_parent_id = metadata.parents[0]
    return LegacyLayoutAssetPlan(
        drive_file_id=asset.drive_file_id or "",
        status=asset.status,
        move_status=asset.move_status,
        scope=asset.scope or "",
        current_name=metadata.name,
        current_parent_id=current_parent_id,
        current_path=asset.remote_path or "",
        target_name=asset.target_name or metadata.name,
        compact_target_path=target_path,
        compact_target_parts=target_parts,
        target_parent_id=target_parent_id,
        trashed=metadata.trashed,
        size=metadata.size,
        mime_type=metadata.mime_type,
        name_changed=metadata.name != (asset.target_name or metadata.name),
        would_add_parent=target_parent_id or str(PurePosixPath(root_folder_id, *target_parts)),
        would_remove_parent=current_parent_id,
        would_set_trashed_false=True,
        database_updates={
            "path_layout_version": PATH_LAYOUT_VERSION,
            "target_path": target_path,
            "target_name": asset.target_name or metadata.name,
            "status": asset.status,
            "move_status": "moved",
            "plan_hash": "recalculate",
        },
    )


def migrate_legacy_layout_assets(
    session: Session,
    drive_client: DriveClient,
    root_folder_id: str,
    expected_file_ids: set[str] | None = None,
    simulate: bool = True,
    cleanup_empty_folders: bool = False,
) -> LegacyLayoutMigrationResult:
    result = LegacyLayoutMigrationResult(simulated=simulate)
    if not acquire_batch_lock(session):
        raise BatchLockUnavailable("managed Drive batch lock is not available")
    try:
        plans = audit_legacy_layout_assets(session, drive_client, root_folder_id)
        result.plans = plans
        result.assets_detected = [plan.drive_file_id for plan in plans]
        if expected_file_ids is not None and set(result.assets_detected) != expected_file_ids:
            raise LegacyLayoutMigrationError("legacy asset set mismatch")
        if simulate:
            result.duplicates = count_duplicate_drive_file_ids(session)
            return result

        old_parent_ids: list[str] = []
        for plan in plans:
            asset = session.scalar(select(Asset).where(Asset.drive_file_id == plan.drive_file_id))
            if asset is None:
                raise LegacyLayoutMigrationError(f"asset disappeared: {plan.drive_file_id}")
            old_parent_ids.append(plan.current_parent_id)
            old_folder = session.scalar(
                select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == plan.current_parent_id)
            )
            target_parent_id, folders_created = ensure_compact_folder_for_legacy(
                session,
                drive_client,
                root_folder_id,
                asset,
                plan.compact_target_parts,
            )
            result.folders_created += folders_created
            before = drive_client.get_file_metadata(plan.drive_file_id)
            if before.size != plan.size or before.mime_type != plan.mime_type:
                raise LegacyLayoutMigrationError(f"file changed before migration: {plan.drive_file_id}")
            drive_client.rename_and_move_file(
                plan.drive_file_id,
                plan.target_name,
                target_parent_id,
                plan.current_parent_id,
            )
            result.drive_mutations += 1
            after = drive_client.get_file_metadata(plan.drive_file_id)
            if not legacy_migration_verified(after, plan, target_parent_id):
                raise LegacyLayoutMigrationError(f"verification failed: {plan.drive_file_id}")
            if old_folder is not None:
                old_folder.item_count = max(0, old_folder.item_count - 1)
            compact_folder = session.scalar(
                select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == target_parent_id)
            )
            if compact_folder is not None:
                compact_folder.item_count += 1
            asset.remote_path = plan.compact_target_path
            asset.source_path = plan.compact_target_path
            asset.target_parent_id = target_parent_id
            asset.target_name = plan.target_name
            asset.path_layout_version = PATH_LAYOUT_VERSION
            asset.move_status = "moved"
            asset.plan_version = PLAN_VERSION
            asset.plan_hash = compute_plan_hash(asset, plan.compact_target_path, plan.target_name, stored_batch_number(asset))
            if asset.status == "review_required":
                result.review_migrated += 1
            elif asset.scope == "generic":
                result.generic_migrated += 1
            elif asset.scope == "brand":
                result.brand_migrated += 1
            elif asset.scope == "title":
                result.title_migrated += 1
            result.assets_migrated += 1
            session.commit()

        if cleanup_empty_folders:
            cleanup_start_ids = [
                *old_parent_ids,
                *session.scalars(
                    select(ManagedDriveFolder.drive_folder_id).where(
                        ManagedDriveFolder.path_layout_version.is_(None),
                        ManagedDriveFolder.item_count == 0,
                    )
                ).all(),
            ]
            cleanup = cleanup_legacy_empty_folders(session, drive_client, cleanup_start_ids, root_folder_id)
            result.folders_deleted = cleanup.folders_deleted
            result.drive_mutations += cleanup.drive_mutations
            result.cleaned_folder_ids = cleanup.cleaned_folder_ids
            result.not_deleted_folder_ids = cleanup.not_deleted_folder_ids
            result.managed_folder_records_deleted = cleanup.managed_folder_records_deleted
        result.duplicates = count_duplicate_drive_file_ids(session)
        return result
    finally:
        release_batch_lock(session)


def compact_target_parts_for_asset(asset: Asset, batch_number: int) -> list[str]:
    media_type = asset.type or "video"
    batch = f"lote-{batch_number:04d}"
    if asset.status == "review_required":
        return ["90_revision", asset.scope or "title", asset.title_slug or slugify(asset.title_name or ""), media_type, batch]
    if asset.scope == "generic":
        return ["10_genericos", media_type, batch]
    if asset.scope == "brand":
        brand_slug = asset.brand.slug if asset.brand else "grandiosa-mujer"
        return ["20_marcas", brand_slug, asset.collection or "evergreen", media_type, batch]
    title_slug = asset.title_slug or slugify(asset.title_name or "")
    if asset.title_type == "series":
        if asset.season_number is not None and asset.episode_number is not None:
            return [
                "30_peliculas_series",
                "series",
                title_slug,
                f"temporada-{asset.season_number:02d}",
                f"episodio-{asset.episode_number:02d}",
                media_type,
                batch,
            ]
        return ["30_peliculas_series", "series", title_slug, "brolls-generales", media_type, batch]
    return ["30_peliculas_series", "peliculas", title_slug, "brolls-generales", media_type, batch]


def existing_compact_folder_id(
    session: Session,
    asset: Asset,
    root_folder_id: str,
    batch_number: int,
) -> str | None:
    context_slug = legacy_context_slug(asset)
    folder = session.scalar(
        select(ManagedDriveFolder).where(
            ManagedDriveFolder.root_folder_id == root_folder_id,
            ManagedDriveFolder.scope == asset.scope,
            ManagedDriveFolder.brand_id == asset.brand_id,
            ManagedDriveFolder.title_slug == asset.title_slug,
            ManagedDriveFolder.title_type == asset.title_type,
            ManagedDriveFolder.collection == asset.collection,
            ManagedDriveFolder.media_type == asset.type,
            ManagedDriveFolder.path_layout_version == PATH_LAYOUT_VERSION,
            ManagedDriveFolder.context_slug == context_slug,
            ManagedDriveFolder.primary_theme.is_(None),
            ManagedDriveFolder.primary_topic == compact_folder_primary_topic(asset),
            ManagedDriveFolder.orientation == "compact",
            ManagedDriveFolder.batch_number == batch_number,
        )
    )
    return folder.drive_folder_id if folder else None


def ensure_compact_folder_for_legacy(
    session: Session,
    drive_client: DriveClient,
    root_folder_id: str,
    asset: Asset,
    target_parts: list[str],
) -> tuple[str, int]:
    batch_number = int(target_parts[-1].removeprefix("lote-"))
    existing = existing_compact_folder_id(session, asset, root_folder_id, batch_number)
    if existing and drive_client.folder_exists(existing):
        return existing, 0
    parent_id = root_folder_id
    created = 0
    for part in target_parts:
        before = drive_client.list_folder(parent_id, 1000)
        parent_id = drive_client.create_folder(parent_id, part)
        if parent_id not in {item.id for item in before}:
            created += 1
    folder = ManagedDriveFolder(
        drive_folder_id=parent_id,
        root_folder_id=root_folder_id,
        scope=asset.scope or "generic",
        brand_id=asset.brand_id,
        title_slug=asset.title_slug,
        title_type=asset.title_type,
        collection=asset.collection,
        media_type=asset.type or "video",
        path_layout_version=PATH_LAYOUT_VERSION,
        context_slug=legacy_context_slug(asset),
        primary_theme=None,
        primary_topic=compact_folder_primary_topic(asset),
        orientation="compact",
        batch_number=batch_number,
    )
    session.add(folder)
    session.flush()
    return parent_id, created


def legacy_migration_verified(metadata: DriveFile, plan: LegacyLayoutAssetPlan, target_parent_id: str) -> bool:
    return (
        metadata.id == plan.drive_file_id
        and metadata.name == plan.target_name
        and target_parent_id in metadata.parents
        and plan.current_parent_id not in metadata.parents
        and metadata.size == plan.size
        and metadata.mime_type == plan.mime_type
        and not metadata.trashed
    )


def count_duplicate_drive_file_ids(session: Session) -> int:
    subquery = (
        select(Asset.drive_file_id)
        .where(Asset.drive_file_id.is_not(None))
        .group_by(Asset.drive_file_id)
        .having(func.count() > 1)
        .subquery()
    )
    return int(session.scalar(select(func.count()).select_from(subquery)) or 0)


PROTECTED_FOLDER_NAMES = {
    "KURUKIN_ASSET_HUB_PILOT",
    "00_entrada",
    "10_genericos",
    "20_marcas",
    "30_peliculas_series",
    "90_revision",
    "99_errores",
}


def cleanup_legacy_empty_folders(
    session: Session,
    drive_client: DriveClient,
    start_folder_ids: list[str],
    root_folder_id: str,
) -> LegacyLayoutMigrationResult:
    result = LegacyLayoutMigrationResult(simulated=False)
    protected_ids = compact_v2_folder_ids(session)
    protected_ids.add(root_folder_id)
    seen: set[str] = set()
    for start_id in start_folder_ids:
        current_id = start_id
        for _depth in range(20):
            if not current_id or current_id in seen or current_id in protected_ids:
                break
            seen.add(current_id)
            try:
                folder = drive_client.get_file_metadata(current_id)
            except ManagedDriveError:
                break
            if folder.name in PROTECTED_FOLDER_NAMES:
                break
            if folder.id in protected_ids:
                break
            if folder_used_by_asset(session, folder.id):
                result.not_deleted_folder_ids.append(folder.id)
                break
            children = drive_client.list_folder(folder.id, 1000)
            if children:
                result.not_deleted_folder_ids.append(folder.id)
                break
            managed_folder = session.scalar(
                select(ManagedDriveFolder).where(ManagedDriveFolder.drive_folder_id == folder.id)
            )
            if managed_folder is not None and managed_folder.item_count != 0:
                result.not_deleted_folder_ids.append(folder.id)
                break
            parent_id = folder.parents[0] if folder.parents else ""
            trash_drive_folder(drive_client, folder.id)
            result.folders_deleted += 1
            result.drive_mutations += 1
            result.cleaned_folder_ids.append(folder.id)
            if managed_folder is not None:
                session.delete(managed_folder)
                result.managed_folder_records_deleted += 1
                session.commit()
            current_id = parent_id
    return result


def compact_v2_folder_ids(session: Session) -> set[str]:
    return set(
        session.scalars(
            select(ManagedDriveFolder.drive_folder_id).where(
                ManagedDriveFolder.path_layout_version == PATH_LAYOUT_VERSION
            )
        ).all()
    )


def folder_used_by_asset(session: Session, folder_id: str) -> bool:
    return bool(session.scalar(select(Asset.id).where(Asset.target_parent_id == folder_id).limit(1)))


def trash_drive_folder(drive_client: DriveClient, folder_id: str) -> None:
    if hasattr(drive_client, "trash_folder"):
        drive_client.trash_folder(folder_id)
        return
    raise LegacyLayoutMigrationError("Drive client does not support folder cleanup")


def compute_plan_hash(asset: Asset, target_path: str, target_name: str, target_batch: int | None) -> str:
    payload = {
        "drive_file_id": asset.drive_file_id,
        "original_name": asset.original_name,
        "original_parent_id": asset.original_parent_id,
        "scope": asset.scope,
        "mime_type": asset.mime_type,
        "media_type": asset.type,
        "source_size_bytes": asset.source_size_bytes,
        "primary_theme": asset.primary_theme,
        "primary_topic": asset.primary_topic,
        "orientation": asset.orientation,
        "target_name": target_name,
        "target_path": target_path,
        "target_batch": target_batch,
        "path_layout_version": asset.path_layout_version,
        "brand_id": asset.brand_id,
        "title_slug": asset.title_slug,
        "title_type": asset.title_type,
        "collection": asset.collection,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stored_batch_number(asset: Asset) -> int | None:
    if not asset.remote_path:
        return None
    if str(asset.remote_path).startswith(f"{REVIEW_PATH}/"):
        return 1
    batch = PurePosixPath(asset.remote_path).parent.name
    if batch.startswith("lote-"):
        try:
            return int(batch.removeprefix("lote-"))
        except ValueError:
            return None
    return None


def assert_persisted_plan_hash(asset: Asset) -> None:
    if not asset.target_name or not asset.remote_path:
        raise MoveError("persisted plan is incomplete")
    expected = compute_plan_hash(asset, asset.remote_path, asset.target_name, stored_batch_number(asset))
    if asset.plan_hash and asset.plan_hash != expected:
        raise MoveError("persisted plan hash mismatch")
    if not asset.plan_hash:
        asset.plan_hash = expected
    asset.plan_version = asset.plan_version or PLAN_VERSION
    asset.plan_created_at = asset.plan_created_at or datetime.now(UTC)


def plan_from_persisted_asset(asset: Asset, request_data: IngestRequest, drive_file: DriveFile) -> IngestPlan:
    tags, tags_source = persisted_asset_tags(asset)
    warnings = ["tags_generated_locally"] if tags_source == "local_derived" else []
    if asset.status == "review_required":
        reason = asset.review_reason or review_reason_for_asset(asset)
        if reason not in warnings:
            warnings.append(reason)
    return IngestPlan(
        drive_file_id=asset.drive_file_id or drive_file.id,
        original_name=asset.original_name or drive_file.name,
        original_parent_id=asset.original_parent_id or (drive_file.parents[0] if drive_file.parents else None),
        source_drive_id=drive_file.drive_id,
        scope=asset.scope or request_data.scope,
        mime_type=asset.mime_type or drive_file.mime_type,
        media_type=asset.type,
        width=asset.width,
        height=asset.height,
        duration=asset.duration_seconds,
        aspect_ratio=aspect_ratio(asset.width, asset.height),
        orientation=asset.orientation,
        title_es=None,
        description_es=asset.description,
        primary_theme=asset.primary_theme or "otros",
        primary_topic=asset.primary_topic or "otros",
        suggested_tags=tags,
        tags_source=tags_source,
        suggested_uses=asset.suggested_uses or [],
        can_flip_horizontal=asset.flip_horizontal_allowed,
        flip_risk_reasons=asset.flip_risk_reasons or [],
        can_zoom=asset.zoom_allowed,
        max_safe_zoom=asset.max_safe_zoom,
        has_visible_text=asset.has_visible_text,
        has_logo=asset.has_logo,
        generic_compatibility=asset.generic_compatibility,
        target_path=asset.remote_path,
        target_name=asset.target_name or asset.filename,
        target_batch=stored_batch_number(asset),
        path_layout_version=asset.path_layout_version,
        preview_path=asset.preview_path,
        thumbnail_path=asset.thumbnail_path,
        classification_confidence=asset.ai_enrichment_confidence,
        warnings=warnings,
        requires_review=asset.status == "review_required",
        database_status=asset.status,
        asset_id=asset.id,
        status=asset.status,
        move_status=asset.move_status,
        ai_analysis_mode="PERSISTED",
        ai_provider=None,
        ai_model=asset.ai_model,
        frames_analyzed=0,
        thumbnail_valid=pilot_preview_is_valid(asset.thumbnail_path),
        thumbnail_size=pilot_preview_size(asset.thumbnail_path),
        plan_version=asset.plan_version,
        plan_created_at=asset.plan_created_at,
        plan_hash=asset.plan_hash,
    )


def update_persisted_people_search_terms(asset: Asset) -> bool:
    updated = False
    analyses = sorted(asset.ai_analyses, key=lambda analysis: analysis.created_at or datetime.min, reverse=True)
    for analysis in analyses:
        result = dict(analysis.result_json or {})
        visual_presentation = str(result.get("visual_presentation") or "")
        person_visibility = str(result.get("person_visibility") or "not_applicable")
        if visual_presentation not in {"masculine", "feminine", "mixed", "unclear", "not_applicable"}:
            continue
        if result.get("source_presentation_hint") is None:
            result["source_presentation_hint"] = source_presentation_hint(asset.original_name or asset.filename)
        terms = people_search_terms(
            visual_presentation,
            person_visibility,
            [str(term) for term in result.get("search_terms") or []],
        )
        if terms != result.get("search_terms"):
            result["search_terms"] = terms
            analysis.result_json = result
            updated = True
        if terms:
            asset.search_text = " ".join([asset.search_text or "", *terms]).strip()
        break
    return updated


def validate_move_preconditions(
    file: DriveFile,
    original_parent_id: str,
    target_parent_id: str,
    drive_client: DriveClient,
) -> None:
    if file.trashed:
        raise MoveError("file is trashed before move")
    if original_parent_id not in file.parents:
        raise MoveError("original parent is not attached to file")
    target = drive_client.get_file_metadata(target_parent_id)
    if not target.is_folder:
        raise MoveError("target parent is not a folder")
    if file.drive_id and target.drive_id and file.drive_id != target.drive_id:
        raise MoveError("target parent is on a different Drive")
    can_move = file.capabilities.get("canMoveItemWithinDrive")
    if can_move is False:
        raise MoveError("Drive capability canMoveItemWithinDrive is false")


def verified_moved_file(
    drive_client: DriveClient,
    file_id: str,
    target_name: str,
    target_parent_id: str,
    original_parent_id: str,
    before: DriveFile,
) -> bool:
    backoff = [1, 2, 4, 8, 10]
    for index, delay in enumerate(backoff):
        metadata = drive_client.get_file_metadata(file_id)
        if (
            metadata.id == file_id
            and metadata.name == target_name
            and not metadata.trashed
            and target_parent_id in metadata.parents
            and original_parent_id not in metadata.parents
            and metadata.size == before.size
            and metadata.mime_type == before.mime_type
        ):
            return True
        if index != len(backoff) - 1:
            time.sleep(delay)
    return False


def build_update_simulation(
    file_id: str,
    original_parent_id: str,
    target_parent_id: str,
    original_name: str,
    target_name: str,
    plan_hash: str | None,
    would_create_folders: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "original_parent_id": original_parent_id,
        "target_parent_id": target_parent_id,
        "original_name": original_name,
        "target_name": target_name,
        "would_create_folders": would_create_folders or [],
        "would_add_parent": target_parent_id,
        "would_remove_parent": original_parent_id,
        "would_set_trashed_false": True,
        "plan_hash": plan_hash,
    }


def get_or_create_pilot_source(session: Session) -> Source:
    source = session.scalar(select(Source).where(Source.source_id == SOURCE_ID))
    if source is not None:
        return source
    source = Source(source_id=SOURCE_ID, provider="google_drive", label="Managed Drive Pilot")
    session.add(source)
    session.flush()
    return source


def resolve_brand(session: Session, request_data: IngestRequest) -> Brand:
    slug = slugify(request_data.brand_slug or "")
    brand = session.scalar(select(Brand).where(Brand.slug == slug))
    if brand is not None:
        return brand
    brand = Brand(slug=slug, name=humanize_slug(slug))
    session.add(brand)
    session.flush()
    return brand


def apply_scope_fields(asset: Asset, request_data: IngestRequest, brand: Brand | None) -> None:
    asset.scope = request_data.scope
    asset.brand = brand
    asset.brand_id = brand.id if brand else None
    asset.collection = request_data.collection if request_data.scope == "brand" else None
    if request_data.scope == "title":
        asset.title_type = request_data.title_type
        asset.title_name = request_data.title
        asset.title_slug = slugify(request_data.title or "")
        asset.season_number = request_data.season
        asset.episode_number = request_data.episode
    else:
        asset.title_type = None
        asset.title_name = None
        asset.title_slug = None
        asset.season_number = None
        asset.episode_number = None


def analyze_technical_metadata(path: Path, drive_file: DriveFile) -> TechnicalResult:
    mime_type = detect_mime_type(path, drive_file)
    media_type = media_type_from_mime(mime_type)
    if media_type not in {"image", "video"}:
        raise typed_error("unsupported_file", f"Unsupported MIME type: {mime_type}")
    try:
        parsed: ParsedProbeMetadata = parse_ffprobe_metadata(run_ffprobe(path))
    except AssetPreviewError as exc:
        raise typed_error("technical_analysis_error", exc) from exc
    return TechnicalResult(
        mime_type=mime_type,
        media_type=media_type,
        width=parsed.width,
        height=parsed.height,
        duration_seconds=parsed.duration_seconds,
        fps=parsed.fps,
        codec=parsed.codec,
        has_audio=parsed.has_audio,
    )


def generate_pilot_previews(asset: Asset, path: Path, technical: TechnicalResult) -> PreviewResult:
    output_dir = Path(get_settings().pilot_preview_root) / str(asset.id)
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    thumbnail = output_dir / "thumbnail.webp"
    preview = output_dir / "preview.webp"
    try:
        if technical.media_type == "video":
            timestamp = 0.5
            if technical.duration_seconds and technical.duration_seconds >= 3:
                timestamp = min(technical.duration_seconds * 0.35, technical.duration_seconds - 0.1)
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:-2",
                    str(thumbnail),
                ],
                "pilot video thumbnail",
            )
            return PreviewResult(thumbnail_path=relative_pilot_preview(asset.id, "thumbnail.webp"), preview_path=None)
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                "scale=w='if(gte(iw,ih),min(360,iw),-2)':h='if(gte(iw,ih),-2,min(360,ih))'",
                "-frames:v",
                "1",
                str(thumbnail),
            ],
            "pilot image thumbnail",
        )
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                "scale=w='if(gte(iw,ih),min(1280,iw),-2)':h='if(gte(iw,ih),-2,min(1280,ih))'",
                "-frames:v",
                "1",
                str(preview),
            ],
            "pilot image preview",
        )
        return PreviewResult(
            thumbnail_path=relative_pilot_preview(asset.id, "thumbnail.webp"),
            preview_path=relative_pilot_preview(asset.id, "preview.webp"),
        )
    except Exception as exc:
        raise typed_error("preview_error", exc) from exc


def pilot_ai_image_inputs(asset: Asset, path: Path, previews: PreviewResult) -> list[Path]:
    if asset.type == "video":
        frame_dir = Path(get_settings().pilot_preview_root) / str(asset.id) / "ai-frames"
        shutil.rmtree(frame_dir, ignore_errors=True)
        frame_dir.mkdir(parents=True, exist_ok=True)
        frames = extract_pilot_video_frames(path, frame_dir, asset.duration_seconds)
        if frames:
            return frames
    image_paths: list[Path] = []
    for rel_path in (previews.preview_path, previews.thumbnail_path):
        local = pilot_preview_file(rel_path)
        if local and local.is_file():
            image_paths.append(local)
    return image_paths or [path]


def extract_pilot_video_frames(path: Path, output_dir: Path, duration_seconds: float | None) -> list[Path]:
    duration = duration_seconds if duration_seconds and duration_seconds > 0 else probe_duration_seconds(path)
    if not duration or duration <= 0:
        return []
    frames: list[Path] = []
    for index, ratio in enumerate((0.25, 0.5, 0.75), start=1):
        timestamp = max(0.0, min(duration * ratio, duration - 0.05))
        output = output_dir / f"frame_{index:03d}.jpg"
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            frames.append(output)
    return frames


def probe_duration_seconds(path: Path) -> float | None:
    try:
        parsed = parse_ffprobe_metadata(run_ffprobe(path))
    except Exception:
        return None
    return parsed.duration_seconds


def count_frame_inputs(paths: list[Path]) -> int:
    return sum(1 for path in paths if path.name.startswith("frame_"))


def configured_ai_provider() -> str:
    settings = get_settings()
    configured = (settings.ai_provider or "").strip()
    if configured:
        return configured
    if settings.nvidia_api_key:
        return "nvidia"
    if settings.openai_api_key:
        return "openai"
    return "none"


def configured_ai_model() -> str | None:
    settings = get_settings()
    if settings.ai_provider == "nvidia":
        return settings.nvidia_model
    if settings.ai_model:
        return settings.ai_model
    if settings.nvidia_api_key:
        return settings.nvidia_model
    return "gpt-4.1-mini" if settings.openai_api_key else None


def enrich_with_existing_ai_system(
    asset: Asset,
    path: Path,
    previews: PreviewResult,
) -> ManagedAIResult:
    settings = get_settings()
    provider = configured_ai_provider()
    model = configured_ai_model()
    if not settings.ai_enrichment_enabled:
        return ManagedAIResult(
            title_es=asset.filename,
            description_es=f"Asset piloto pendiente de enrichment IA: {asset.filename}",
            primary_theme="otros",
            primary_topic=asset.type or "asset",
            tags=["piloto"],
            tags_source="local_derived",
            suggested_uses=["revision"],
            confidence={"overall": 0.0},
            ai_analysis_mode="FALLBACK",
            ai_provider=provider,
            ai_model=model,
            frames_analyzed=0,
        )
    image_paths = pilot_ai_image_inputs(asset, path, previews)
    prompt = build_asset_enrichment_prompt(asset)
    try:
        result = call_openai_vision(prompt, image_paths)
    except Exception as exc:
        raise typed_error("ai_error", sanitize_provider_error(str(exc))) from exc
    return managed_result_from_existing(
        result,
        provider=provider,
        model=model,
        frames_analyzed=count_frame_inputs(image_paths),
    )


def managed_result_from_existing(
    result: AIAssetEnrichmentResult,
    provider: str | None = None,
    model: str | None = None,
    frames_analyzed: int = 0,
) -> ManagedAIResult:
    keyword_values = [keyword.keyword for keyword in result.keywords]
    subject = next((keyword.keyword for keyword in result.keywords if keyword.category == "subject"), None)
    action = next((keyword.keyword for keyword in result.keywords if keyword.category == "action"), None)
    context = next((keyword.keyword for keyword in result.keywords if keyword.category in {"location", "context"}), None)
    topic = subject or result.similarity_group or result.title or "otros"
    warnings = split_text_list(result.review_reason)
    return ManagedAIResult(
        title_es=result.title_es or result.title,
        description_es=result.description_es or result.visual_description or result.embedding_text,
        primary_theme=result.primary_theme or topic,
        primary_topic=result.primary_topic or topic,
        subject=result.subject or subject,
        action=result.action or action or result.action_description,
        context=result.context or context or result.location,
        tags=keyword_values,
        tags_source="nvidia" if keyword_values else None,
        suggested_uses=result.suggested_uses or split_text_list(result.best_for),
        contains_people=result.contains_people,
        people_count=result.people_count,
        visual_presentation=result.visual_presentation,
        visual_presentation_confidence=result.visual_presentation_confidence,
        person_visibility=result.person_visibility,
        search_terms=result.search_terms,
        source_presentation_hint=result.source_presentation_hint,
        can_flip_horizontal=result.flip_horizontal_allowed,
        flip_risk_reasons=split_text_list(result.avoid_for) if not result.flip_horizontal_allowed else [],
        can_zoom=result.zoom_allowed,
        max_safe_zoom=1.2 if result.zoom_allowed else 1.0,
        has_visible_text=result.has_visible_text,
        has_logo=result.has_logo,
        generic_compatibility=not result.has_logo and not result.has_watermark,
        camera_motion=result.camera_motion,
        shot_type=result.shot_type,
        subject_position=result.subject_position,
        safe_text_areas=[result.overlay_safe_area],
        confidence={"overall": result.confidence, "primary_theme": result.confidence},
        requires_review=bool(result.needs_human_review),
        warnings=warnings,
        ai_analysis_mode="REAL",
        ai_provider=provider,
        ai_model=model,
        frames_analyzed=frames_analyzed,
    )


def normalize_managed_people_metadata(ai_result: ManagedAIResult, original_name: str | None) -> ManagedAIResult:
    source_hint = source_presentation_hint(original_name)
    visual_presentation = ai_result.visual_presentation
    if (
        visual_presentation in {"masculine", "feminine"}
        and ai_result.person_visibility in {"back_view", "silhouette", "occluded"}
        and not gendered_metadata_allowed(
            ai_result.visual_presentation_confidence,
            ai_result.person_visibility,
        )
    ):
        visual_presentation = "unclear"
    search_terms = people_search_terms(
        visual_presentation,
        ai_result.person_visibility,
        ai_result.search_terms,
    )
    updates: dict[str, Any] = {
        "visual_presentation": visual_presentation,
        "source_presentation_hint": source_hint,
        "search_terms": search_terms,
    }
    if not ai_result.contains_people:
        updates.update(
            {
                "people_count": None,
                "visual_presentation": "not_applicable",
                "person_visibility": "not_applicable",
                "search_terms": [],
            }
        )
    if not gendered_metadata_allowed(
        ai_result.visual_presentation_confidence,
        ai_result.person_visibility,
    ):
        updates.update(
            {
                "title_es": neutralize_gendered_terms(ai_result.title_es),
                "primary_topic": neutralize_gendered_terms(ai_result.primary_topic),
                "subject": neutralize_gendered_terms(ai_result.subject),
                "context": neutralize_gendered_terms(ai_result.context),
                "tags": neutralize_gendered_tags(ai_result.tags),
            }
        )
    return ai_result.model_copy(update=updates)


TAG_STOPWORDS = {
    "a",
    "al",
    "ante",
    "con",
    "contra",
    "de",
    "del",
    "desde",
    "durante",
    "e",
    "el",
    "ella",
    "en",
    "entre",
    "es",
    "esa",
    "ese",
    "esta",
    "este",
    "frente",
    "hacia",
    "la",
    "las",
    "lo",
    "los",
    "para",
    "por",
    "que",
    "se",
    "sin",
    "sobre",
    "su",
    "sus",
    "un",
    "una",
    "unas",
    "unos",
    "y",
}
TAG_GENERIC_TERMS = {"archivo", "asset", "escena", "imagen", "video", "visual"}


def ensure_managed_tags(ai_result: ManagedAIResult, classification: Classification) -> tuple[ManagedAIResult, list[str]]:
    existing = unique_tags(ai_result.tags)
    if not gendered_metadata_allowed(ai_result.visual_presentation_confidence, ai_result.person_visibility):
        existing = neutralize_gendered_tags(existing)
    if existing:
        return ai_result.model_copy(update={"tags": existing[:10], "tags_source": ai_result.tags_source or "nvidia"}), []
    tags = derive_local_tags(ai_result, classification)
    if not tags:
        tags = unique_tags([classification.primary_theme, classification.primary_topic, "biblioteca visual"])
    return ai_result.model_copy(update={"tags": tags[:10], "tags_source": "local_derived"}), ["tags_generated_locally"]


def derive_local_tags(ai_result: ManagedAIResult, classification: Classification) -> list[str]:
    candidates: list[str] = [classification.primary_theme.replace("-", " "), classification.primary_topic]
    candidates.extend(tokenize_tag_text(ai_result.title_es or ""))
    candidates.extend(tokenize_tag_text(ai_result.description_es or ""))
    tags = unique_tags(candidates)[:10]
    if not gendered_metadata_allowed(ai_result.visual_presentation_confidence, ai_result.person_visibility):
        tags = neutralize_gendered_tags(tags)
    return tags


def tokenize_tag_text(value: str) -> list[str]:
    normalized = re.sub(r"[^\wáéíóúüñÁÉÍÓÚÜÑ]+", " ", value.lower(), flags=re.UNICODE)
    words = [word for word in normalized.split() if useful_tag_word(word)]
    tags: list[str] = []
    tags.extend(words)
    for left, right in zip(words, words[1:], strict=False):
        if left not in TAG_STOPWORDS and right not in TAG_STOPWORDS:
            tags.append(f"{left} {right}")
    return tags


def useful_tag_word(value: str) -> bool:
    word = value.strip().lower()
    if len(word) < 3:
        return False
    if word in TAG_STOPWORDS or word in TAG_GENERIC_TERMS:
        return False
    return True


def unique_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = re.sub(r"\s+", " ", str(value).strip().lower())
        if not tag or tag in TAG_GENERIC_TERMS or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= 10:
            break
    return tags


def persisted_asset_tags(asset: Asset) -> tuple[list[str], str | None]:
    analyses = sorted(asset.ai_analyses, key=lambda analysis: analysis.created_at or datetime.min, reverse=True)
    for analysis in analyses:
        result = analysis.result_json or {}
        tags = unique_tags([str(tag) for tag in result.get("tags") or []])
        source = result.get("tags_source")
        if tags:
            return tags, str(source or "nvidia")
        managed = ManagedAIResult(
            title_es=asset.title_name or asset.filename,
            description_es=asset.description,
            primary_theme=asset.primary_theme,
            primary_topic=asset.primary_topic,
            tags=[],
        )
        derived = derive_local_tags(
            managed,
            Classification(asset.primary_theme or "otros", asset.primary_topic or "otros"),
        )
        if derived:
            return derived, "local_derived"
    return [], None


REVIEW_REASON_PRIORITY = (
    "ai_response_invalid",
    "classification_ambiguous",
    "ai_requires_review",
    "taxonomy_low_confidence",
    "ai_classification_insufficient",
    "theme_semantically_inconsistent",
    "logo_or_text_risk",
)


def review_reason_from_warnings(
    warnings: list[str],
    ai_result: ManagedAIResult | None = None,
    classification: Classification | None = None,
    insufficient_ai: bool = False,
) -> str | None:
    reasons = list(warnings)
    if classification and classification.ambiguous and "classification_ambiguous" not in reasons:
        reasons.append("classification_ambiguous")
    if insufficient_ai and "ai_classification_insufficient" not in reasons:
        reasons.append("ai_classification_insufficient")
    if ai_result and (ai_result.has_visible_text or ai_result.has_logo) and "logo_or_text_risk" not in reasons:
        reasons.append("logo_or_text_risk")
    reason_set = set(reasons)
    for reason in REVIEW_REASON_PRIORITY:
        if reason in reason_set:
            return reason
    return next((reason for reason in reasons if reason != "tags_generated_locally"), None)


def review_reason_for_asset(asset: Asset) -> str:
    if asset.ai_enrichment_confidence is not None and asset.ai_enrichment_confidence < get_settings().ai_review_threshold:
        return "classification_ambiguous"
    if asset.has_visible_text or asset.has_logo:
        return "logo_or_text_risk"
    return "review_required"


def controlled_classification(ai_result: ManagedAIResult) -> Classification:
    theme = slugify(ai_result.primary_theme or "")
    topic = normalize_primary_topic(ai_result.primary_topic or "")
    confidence = ai_confidence(ai_result)
    if theme not in PRIMARY_THEMES:
        theme = infer_theme_from_topic(slugify(topic))
    if not topic:
        topic = "otros"
    topic = trim_primary_topic(topic, 80) or "otros"
    ambiguous = confidence < get_settings().ai_review_threshold
    if not valid_primary_topic(topic):
        topic = "otros"
        ambiguous = True
    return Classification(theme, topic, ambiguous=ambiguous)


def ai_classification_insufficient(ai_result: ManagedAIResult, classification: Classification) -> bool:
    if ai_result.ai_analysis_mode != "REAL":
        return True
    if classification.primary_theme == "otros" and classification.primary_topic in {"video", "imagen", "asset", "archivo", "otros"}:
        return True
    if classification.primary_topic in {"video", "imagen", "asset", "archivo"}:
        return True
    if ai_result.frames_analyzed <= 0 and ai_result.ai_analysis_mode == "REAL":
        return True
    return False


def infer_theme_from_topic(topic: str) -> str:
    if any(word in topic for word in ("persona", "gente", "mujer", "hombre", "nino", "familia")):
        return "personas"
    if any(word in topic for word in ("yoga", "salud", "bienestar", "medicina")):
        return "salud-bienestar"
    if any(word in topic for word in ("ciudad", "edificio", "arquitectura", "calle")):
        return "ciudad-arquitectura"
    if any(word in topic for word in ("comida", "alimento", "cocina")):
        return "alimentos"
    return "otros"


def normalize_primary_topic(value: str | None) -> str:
    normalized = (value or "").replace("-", " ").replace("_", " ").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def primary_topic_slug(value: str | None) -> str:
    return slugify(normalize_primary_topic(value))


def valid_primary_topic(value: str | None) -> bool:
    topic = normalize_primary_topic(value)
    if not topic:
        return False
    return 2 <= len(topic.split()) <= 8


def trim_primary_topic(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    words = []
    for word in value.split():
        candidate = " ".join([*words, word])
        if len(candidate) > limit:
            break
        words.append(word)
    return " ".join(words) or value[:limit].strip()


def generic_route_topic(topic: str, media_type: str, theme: str) -> bool:
    topic_slug = primary_topic_slug(topic)
    return topic_slug in {media_type, "video", "imagen", "image", "asset", "archivo", "otros", theme}


def next_batch_number(
    session: Session,
    request_data: IngestRequest,
    asset: Asset,
    classification: Classification,
    requires_review: bool = False,
    exclude_asset_id: int | None = None,
) -> int:
    batch_size = get_settings().managed_folder_batch_size
    context_slug = folder_context_slug(request_data, asset)
    latest = session.scalar(
        select(ManagedDriveFolder)
        .where(
            ManagedDriveFolder.root_folder_id == request_data.destination_root_id,
            ManagedDriveFolder.scope == request_data.scope,
            ManagedDriveFolder.brand_id == asset.brand_id,
            ManagedDriveFolder.title_slug == asset.title_slug,
            ManagedDriveFolder.title_type == asset.title_type,
            ManagedDriveFolder.collection == asset.collection,
            ManagedDriveFolder.media_type == asset.type,
            ManagedDriveFolder.path_layout_version == PATH_LAYOUT_VERSION,
            ManagedDriveFolder.context_slug == context_slug,
            ManagedDriveFolder.primary_theme.is_(None),
            ManagedDriveFolder.primary_topic == compact_folder_primary_topic(asset),
            ManagedDriveFolder.orientation == "compact",
        )
        .order_by(ManagedDriveFolder.batch_number.desc())
        .limit(1)
    )
    candidate = 1
    if latest is not None:
        candidate = latest.batch_number + 1 if latest.item_count >= batch_size else latest.batch_number
    while compact_batch_asset_count(
        session,
        request_data,
        asset,
        classification,
        candidate,
        requires_review=requires_review,
        exclude_asset_id=exclude_asset_id,
    ) >= batch_size:
        candidate += 1
    return candidate


def compact_batch_asset_count(
    session: Session,
    request_data: IngestRequest,
    asset: Asset,
    classification: Classification,
    batch_number: int,
    requires_review: bool = False,
    exclude_asset_id: int | None = None,
) -> int:
    target_parts = route_parts(
        request_data,
        asset.brand,
        asset.type,
        classification,
        asset.orientation,
        batch_number,
        requires_review=requires_review,
        asset=asset,
    )
    prefix = str(PurePosixPath(*target_parts)) + "/"
    query = select(Asset).where(
        Asset.path_layout_version == PATH_LAYOUT_VERSION,
        Asset.scope == request_data.scope,
        Asset.type == asset.type,
        Asset.remote_path.like(prefix + "%"),
        Asset.move_status.in_(["planned", "moving", "moved", "verification_required"]),
    )
    if exclude_asset_id is not None:
        query = query.where(Asset.id != exclude_asset_id)
    return len(session.scalars(query).all())


def target_filename(
    request_data: IngestRequest,
    brand: Brand | None,
    asset: Asset,
    ai_result: ManagedAIResult,
) -> str:
    ext = PurePosixPath(asset.original_name or asset.filename).suffix.lower()
    if not ext:
        ext = mimetypes.guess_extension(asset.mime_type or "") or ""
    semantic_parts = semantic_filename_parts(ai_result)
    suffix = short_id(asset.drive_file_id or asset.asset_uid)
    if request_data.scope == "generic":
        stem = "-".join(semantic_parts)
    elif request_data.scope == "brand":
        prefix = brand.slug if brand else request_data.brand_slug
        semantic_parts = strip_existing_scope_prefix(semantic_parts, prefix)
        stem = "-".join(filter(None, [prefix, *semantic_parts]))
    else:
        prefix = slugify(request_data.title or "")
        semantic_parts = strip_existing_scope_prefix(semantic_parts, prefix)
        stem = "-".join(filter(None, [prefix, *semantic_parts]))
    stem = trim_semantic_stem(stem, 80) or "asset"
    orientation = compact_orientation(asset.orientation)
    return f"{stem}__{orientation}__{suffix}{ext}"


def strip_existing_scope_prefix(parts: list[str], prefix: str | None) -> list[str]:
    if not parts or not prefix:
        return parts
    joined = "-".join(parts)
    if joined == prefix:
        return []
    if joined.startswith(f"{prefix}-"):
        stripped = joined.removeprefix(f"{prefix}-")
        return [stripped] if stripped else []
    return parts


def semantic_filename_parts(ai_result: ManagedAIResult) -> list[str]:
    allow_gendered = gendered_metadata_allowed(
        ai_result.visual_presentation_confidence,
        ai_result.person_visibility,
    )
    candidates = [
        ai_result.subject,
        ai_result.action,
        ai_result.context,
        None if ai_result.shot_type == "unknown" else shot_type_es(ai_result.shot_type),
    ]
    parts: list[str] = []
    for candidate in candidates:
        if not allow_gendered:
            candidate = neutralize_gendered_terms(candidate)
        part = clean_semantic_slug(candidate)
        if part and part not in parts:
            parts.append(part)
    if parts:
        return parts
    fallback_text = ai_result.title_es or ai_result.primary_topic or ""
    if not allow_gendered:
        fallback_text = neutralize_gendered_terms(fallback_text) or ""
    fallback = clean_semantic_slug(fallback_text)
    if not is_meaningful_naming_text(fallback):
        return []
    return [fallback] if fallback else []


def valid_target_filename(value: str | None) -> bool:
    stem = existing_semantic_stem(value)
    return is_meaningful_naming_text(stem)


def usage_policy_for_request(request_data: IngestRequest, ai_result: ManagedAIResult) -> dict[str, Any]:
    if request_data.scope == "generic":
        return {"allowed_default_mix": ["generic"]}
    if request_data.scope == "brand":
        return {
            "allowed_default_mix": ["brand:self", "generic"],
            "deny_other_brands": True,
            "deny_titles": True,
        }
    return {
        "allowed_default_mix": ["title:self"],
        "generic_compatibility": bool(ai_result.generic_compatibility),
        "deny_brands": True,
    }


def persist_ai_analysis(session: Session, asset: Asset, ai_result: ManagedAIResult) -> None:
    result_json = ai_result.model_dump(mode="json")
    result_json["ai_analysis_mode"] = ai_result.ai_analysis_mode
    result_json["frames_analyzed"] = ai_result.frames_analyzed
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=ai_result.ai_model or "managed-drive-pilot",
            provider=ai_result.ai_provider or get_settings().ai_provider,
            input_type="frames" if ai_result.frames_analyzed else "preview",
            prompt_version=ASSET_ENRICHMENT_PROMPT_VERSION,
            result_json=result_json,
            confidence=ai_confidence(ai_result),
        )
    )


def ai_confidence(ai_result: ManagedAIResult) -> float:
    values = [float(value) for value in ai_result.confidence.values() if isinstance(value, int | float)]
    if not values:
        return 0.0
    return sum(values) / len(values)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mime_type(path: Path, drive_file: DriveFile) -> str:
    guessed = drive_file.mime_type or mimetypes.guess_type(drive_file.name)[0]
    if guessed:
        return guessed
    try:
        result = subprocess.run(
            ["file", "--brief", "--mime-type", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return "application/octet-stream"
    return result.stdout.strip() or "application/octet-stream"


def media_type_from_mime(mime_type: str) -> str:
    if mime_type in SUPPORTED_IMAGE_MIMES or mime_type.startswith("image/"):
        return "image"
    if mime_type in SUPPORTED_VIDEO_MIMES or mime_type.startswith("video/"):
        return "video"
    return "unknown"


def relative_pilot_preview(asset_id: int, filename: str) -> str:
    return f"pilot-previews/{asset_id}/{filename}"


def pilot_preview_file(path: str | None) -> Path | None:
    if not path or not path.startswith("pilot-previews/"):
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) != 3:
        return None
    return Path(get_settings().pilot_preview_root) / parts[1] / parts[2]


def pilot_preview_size(path: str | None) -> int | None:
    local = pilot_preview_file(path)
    if local is None or not local.is_file():
        return None
    return local.stat().st_size


def pilot_preview_is_valid(path: str | None) -> bool:
    local = pilot_preview_file(path)
    if local is None or not local.is_file() or local.stat().st_size <= 0:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(local)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return any(stream.get("codec_type") == "video" for stream in payload.get("streams", []))


def asset_matches_drive_file(asset: Asset, drive_file: DriveFile) -> bool:
    if drive_file.size is not None and asset.source_size_bytes not in (None, drive_file.size):
        return False
    if drive_file.modified_time is not None and asset.source_modified_at not in (None, drive_file.modified_time):
        return False
    return True


def plan_from_ready_asset(asset: Asset, request_data: IngestRequest) -> IngestPlan:
    tags, tags_source = persisted_asset_tags(asset)
    return IngestPlan(
        drive_file_id=asset.drive_file_id or "",
        original_name=asset.original_name or asset.filename,
        original_parent_id=asset.original_parent_id,
        source_drive_id=None,
        scope=asset.scope or request_data.scope,
        brand=asset.brand.slug if asset.brand else request_data.brand_slug,
        title=asset.title_name,
        collection=asset.collection,
        mime_type=asset.mime_type,
        media_type=asset.type,
        width=asset.width,
        height=asset.height,
        duration=asset.duration_seconds,
        aspect_ratio=aspect_ratio(asset.width, asset.height),
        orientation=asset.orientation,
        title_es=asset.title_name or asset.filename,
        description_es=asset.description,
        primary_theme=asset.primary_theme or "otros",
        primary_topic=asset.primary_topic or "otros",
        suggested_tags=tags,
        tags_source=tags_source,
        suggested_uses=asset.suggested_uses or [],
        can_flip_horizontal=asset.flip_horizontal_allowed,
        flip_risk_reasons=asset.flip_risk_reasons or [],
        can_zoom=asset.zoom_allowed,
        max_safe_zoom=asset.max_safe_zoom,
        has_visible_text=asset.has_visible_text,
        has_logo=asset.has_logo,
        generic_compatibility=asset.generic_compatibility,
        target_path=asset.remote_path,
        target_name=asset.target_name or asset.filename,
        target_batch=stored_batch_number(asset),
        path_layout_version=asset.path_layout_version,
        preview_path=asset.preview_path,
        thumbnail_path=asset.thumbnail_path,
        classification_confidence=asset.ai_enrichment_confidence,
        warnings=["already_ready", *(["tags_generated_locally"] if tags_source == "local_derived" else [])],
        requires_review=asset.status == "review_required",
        database_status=asset.status,
        asset_id=asset.id,
        status=asset.status,
        move_status=asset.move_status,
        ai_analysis_mode="REAL" if asset.ai_enrichment_status in {"ready", "needs_review"} else "FALLBACK",
        ai_provider=None,
        ai_model=asset.ai_model,
        frames_analyzed=0,
        thumbnail_valid=bool(asset.thumbnail_path and pilot_preview_is_valid(asset.thumbnail_path)),
        thumbnail_size=pilot_preview_size(asset.thumbnail_path),
        result="already_ready",
    )


def failed_plan(drive_file: DriveFile, request_data: IngestRequest, exc: Exception) -> IngestPlan:
    return IngestPlan(
        drive_file_id=drive_file.id,
        original_name=drive_file.name,
        original_parent_id=drive_file.parents[0] if drive_file.parents else request_data.source_folder_id,
        source_drive_id=drive_file.drive_id,
        scope=request_data.scope,
        mime_type=drive_file.mime_type,
        media_type="unknown",
        width=None,
        height=None,
        duration=None,
        aspect_ratio=None,
        orientation="otro",
        title_es=None,
        description_es=None,
        primary_theme="otros",
        primary_topic="otros",
        suggested_tags=[],
        suggested_uses=[],
        can_flip_horizontal=False,
        flip_risk_reasons=[],
        can_zoom=False,
        max_safe_zoom=None,
        has_visible_text=False,
        has_logo=False,
        generic_compatibility=False,
        target_path="99_errores",
        target_name=drive_file.name,
        target_batch=None,
        path_layout_version=None,
        preview_path=None,
        thumbnail_path=None,
        classification_confidence=None,
        warnings=[f"{error_type(exc)}: {sanitize_error_message(str(exc))}"],
        requires_review=True,
        database_status="failed",
        brand=request_data.brand_slug,
        title=request_data.title,
        collection=request_data.collection if request_data.scope == "brand" else None,
        status="failed",
        move_status="not_planned",
        ai_analysis_mode="FALLBACK",
        ai_provider=None,
        ai_model=None,
        frames_analyzed=0,
        thumbnail_valid=False,
        thumbnail_size=None,
    )


def resolve_applied_idempotence(
    session: Session,
    request_data: IngestRequest,
    drive_client: DriveClient,
) -> IngestPlan | None:
    asset = session.scalar(select(Asset).where(Asset.drive_file_id == request_data.file_id))
    if not persisted_moved_asset_is_complete(asset):
        return None
    assert asset is not None
    try:
        drive_file = drive_client.get_file_metadata(asset.drive_file_id or "")
    except Exception as exc:
        differences = [f"files.get failed: {sanitize_error_message(str(exc))}"]
        return mark_idempotence_mismatch(session, asset, request_data, differences)
    differences = moved_asset_drive_differences(asset, drive_file)
    if differences:
        return mark_idempotence_mismatch(session, asset, request_data, differences)
    return replace(
        plan_from_ready_asset(asset, request_data),
        result="already_applied",
        status=asset.status,
        move_status="moved",
        database_status=asset.status,
        requires_review=asset.status == "review_required",
        drive_mutations_performed=0,
        mutation_stats=MutationStats().to_dict(),
        warnings=[],
        ai_analysis_mode="PERSISTED",
    )


def persisted_moved_asset_is_complete(asset: Asset | None) -> bool:
    return bool(
        asset
        and asset.status in {"ready", "review_required"}
        and asset.move_status == "moved"
        and asset.target_name
        and asset.remote_path
    )


def moved_asset_drive_differences(asset: Asset, drive_file: DriveFile) -> list[str]:
    differences = []
    if drive_file.id != asset.drive_file_id:
        differences.append(f"fileId: Drive={drive_file.id!r} PostgreSQL={asset.drive_file_id!r}")
    if drive_file.trashed:
        differences.append("trashed: Drive=true expected=false")
    if drive_file.name != asset.target_name:
        differences.append(f"name: Drive={drive_file.name!r} PostgreSQL target_name={asset.target_name!r}")
    if not asset.target_parent_id:
        differences.append("target_parent_id: PostgreSQL is empty")
    elif set(drive_file.parents) != {asset.target_parent_id}:
        differences.append(
            f"parents: Drive={drive_file.parents!r} PostgreSQL target_parent_id={asset.target_parent_id!r}"
        )
    if drive_file.size != asset.source_size_bytes:
        differences.append(f"size: Drive={drive_file.size!r} PostgreSQL source_size_bytes={asset.source_size_bytes!r}")
    if drive_file.mime_type != asset.mime_type:
        differences.append(f"mimeType: Drive={drive_file.mime_type!r} PostgreSQL mime_type={asset.mime_type!r}")
    return differences


def mark_idempotence_mismatch(
    session: Session,
    asset: Asset,
    request_data: IngestRequest,
    differences: list[str],
) -> IngestPlan:
    asset.status = "review_required"
    asset.move_status = "verification_required"
    asset.review_reason = "idempotence_state_mismatch: " + "; ".join(differences)
    session.commit()
    return replace(
        plan_from_ready_asset(asset, request_data),
        result="state_mismatch",
        status="review_required",
        move_status="verification_required",
        database_status="review_required",
        requires_review=True,
        warnings=differences,
        drive_mutations_performed=0,
        mutation_stats=MutationStats().to_dict(),
        ai_analysis_mode="PERSISTED",
    )


def preflight_drive_access(
    request_data: PreflightRequest,
    drive_client: DriveClient | None = None,
) -> PreflightResult:
    settings = get_settings()
    drive_client = drive_client or drive_client_from_environment()
    source = drive_client.get_file_metadata(request_data.source_folder_id)
    destination = drive_client.get_file_metadata(request_data.destination_root_id)
    listed_files = drive_client.list_folder(request_data.source_folder_id, 1)
    test_file = find_preflight_file(drive_client, request_data, listed_files)
    source_drive = source.drive_id or "my-drive"
    destination_drive = destination.drive_id or "my-drive"
    same_drive = source_drive == destination_drive
    legacy_ids = sorted(
        {
            item
            for item in configured_legacy_folder_ids(settings)
            if item in {request_data.source_folder_id, request_data.destination_root_id}
        }
    )
    warnings = []
    if test_file is None:
        warnings.append("no_test_file_available")
    if not same_drive:
        warnings.append("source_and_destination_are_on_different_drives")
    auth_mode = getattr(drive_client, "auth_mode", settings.google_drive_auth_mode.strip().lower() or "adc")
    if auth_mode == "access_token":
        warnings.append("access_token_auth_is_temporary")
    auth_status = drive_auth_status(settings, drive_client)
    can_create = destination.capabilities.get("canAddChildren", False)
    can_update = can_update_metadata(test_file)
    can_move = can_move_test_file(test_file)
    can_restore = can_restore_from_trash(test_file)
    oauth_scope_sufficient = auth_status["oauth_scope_sufficient"]
    scopes_complete = (
        can_create
        and can_move
        and can_update
        and can_change_parents(test_file)
        and can_restore
        and bool(test_file and not test_file.trashed)
        and same_drive
        and shared_drive_supported(source, destination, test_file)
        and auth_status["auth_refreshable"]
        and oauth_scope_sufficient
    )
    return PreflightResult(
        auth_mode=auth_mode,
        source_folder="OK" if source.is_folder else "NOT_FOLDER",
        source_drive=source_drive,
        destination_folder="OK" if destination.is_folder else "NOT_FOLDER",
        destination_drive=destination_drive,
        same_drive=same_drive,
        can_list_source=True,
        can_create_in_destination=can_create,
        can_move_files=can_move,
        test_file_id=test_file.id if test_file else None,
        destination_inside_pilot_root=destination_inside_pilot_root(
            drive_client,
            request_data.destination_root_id,
            settings.google_drive_root_folder_id,
        ),
        legacy_ids_detected=legacy_ids,
        scopes_complete=scopes_complete and drive_scope_config_complete(settings),
        warnings=warnings,
        auth_refreshable=auth_status["auth_refreshable"],
        custom_oauth_client=auth_status["custom_oauth_client"],
        source_accessible=source.is_folder,
        root_accessible=destination.is_folder,
        file_accessible=test_file is not None,
        file_trashed=test_file.trashed if test_file else None,
        can_update=can_update,
        can_move_within_drive=can_move,
        can_add_children=can_create,
        can_restore_from_trash=can_restore,
        oauth_scope_sufficient=oauth_scope_sufficient,
    )


def find_preflight_file(
    drive_client: DriveClient,
    request_data: PreflightRequest,
    listed_files: list[DriveFile],
) -> DriveFile | None:
    if request_data.file_id:
        try:
            drive_file = drive_client.get_file_metadata(request_data.file_id)
            if not (drive_file.is_folder and not drive_file.name):
                return drive_file
        except ManagedDriveError:
            pass
        for drive_file in drive_client.list_folder(request_data.source_folder_id, max(100, len(listed_files))):
            if drive_file.id == request_data.file_id:
                return drive_file
        return None
    return listed_files[0] if listed_files else None


def can_move_test_file(file: DriveFile | None) -> bool:
    if file is None:
        return False
    if file.is_folder:
        return False
    can_move = file.capabilities.get("canMoveItemWithinDrive")
    return can_move is not False and file.capabilities.get("canEdit") is not False


def can_update_metadata(file: DriveFile | None) -> bool:
    return bool(file and file.capabilities.get("canEdit") is True)


def can_change_parents(file: DriveFile | None) -> bool:
    return bool(file and file.capabilities.get("canMoveItemWithinDrive") is not False)


def can_restore_from_trash(file: DriveFile | None) -> bool:
    if file is None:
        return False
    can_untrash = file.capabilities.get("canUntrash")
    if can_untrash is None:
        return file.capabilities.get("canEdit") is True
    return can_untrash is True


def shared_drive_supported(source: DriveFile, destination: DriveFile, test_file: DriveFile | None) -> bool:
    if not (source.drive_id or destination.drive_id or (test_file.drive_id if test_file else None)):
        return True
    return source.drive_id == destination.drive_id and (test_file is None or test_file.drive_id == source.drive_id)


def destination_inside_pilot_root(
    drive_client: DriveClient,
    destination_root_id: str,
    expected_root_id: str | None,
) -> bool:
    if not expected_root_id:
        return False
    if destination_root_id == expected_root_id:
        return True
    current_id = destination_root_id
    seen: set[str] = set()
    for _depth in range(20):
        if current_id in seen:
            return False
        seen.add(current_id)
        current = drive_client.get_file_metadata(current_id)
        if expected_root_id in current.parents:
            return True
        if not current.parents:
            return False
        current_id = current.parents[0]
    return False


def drive_scope_config_complete(settings: Any) -> bool:
    required = [
        settings.google_drive_root_folder_id,
        settings.google_drive_generic_inbox_folder_id,
        settings.google_drive_brand_inbox_folder_id,
        settings.google_drive_title_inbox_folder_id,
    ]
    return all(bool(value and str(value).strip()) for value in required)


def drive_auth_status(settings: Any, drive_client: DriveClient) -> dict[str, bool]:
    status = {
        "auth_refreshable": False,
        "custom_oauth_client": False,
        "oauth_scope_sufficient": False,
    }
    if isinstance(drive_client, RcloneDriveClient):
        status.update(rclone_remote_auth_status(drive_client.remote, drive_client._source_config_path))
        return status
    if not isinstance(drive_client, GoogleDriveAPIClient):
        return {
            "auth_refreshable": True,
            "custom_oauth_client": True,
            "oauth_scope_sufficient": True,
        }
    auth_mode = getattr(drive_client, "auth_mode", settings.google_drive_auth_mode.strip().lower() or "")
    if auth_mode == "adc":
        credentials = getattr(drive_client, "credentials", None)
        status["auth_refreshable"] = bool(
            getattr(credentials, "refresh_token", None) or getattr(credentials, "service_account_email", None)
        )
        status["oauth_scope_sufficient"] = True
    if auth_mode == "access_token":
        status["oauth_scope_sufficient"] = True
    return status


def rclone_remote_auth_status(remote: str, config_path: Path | None) -> dict[str, bool]:
    status = {
        "auth_refreshable": False,
        "custom_oauth_client": False,
        "oauth_scope_sufficient": False,
    }
    config_path = config_path or rclone_config_path()
    if config_path is None or not config_path.exists():
        return status
    parser = configparser.ConfigParser()
    parser.read(config_path)
    remote_name = remote.strip().rstrip(":")
    if remote_name not in parser:
        return status
    section = parser[remote_name]
    status["custom_oauth_client"] = bool(section.get("client_id") and section.get("client_secret"))
    status["oauth_scope_sufficient"] = (section.get("scope") or "").strip() == "drive"
    try:
        token = json.loads(section.get("token") or "{}")
    except json.JSONDecodeError:
        token = {}
    status["auth_refreshable"] = bool(token.get("refresh_token"))
    return status


def configured_legacy_folder_ids(settings: Any) -> list[str]:
    value = settings.google_drive_legacy_folder_ids or ""
    return [item.strip() for item in value.split(",") if item.strip()]


def typed_error(error_name: str, detail: object) -> ManagedDriveError:
    error = ManagedDriveError(sanitize_error_message(str(detail)))
    error.error_type = error_name
    return error


def error_type(exc: Exception) -> str:
    return getattr(exc, "error_type", exc.__class__.__name__)


class RcloneDriveClient:
    def __init__(self, remote: str, config_path: Path | None = None) -> None:
        self.remote = remote.strip().rstrip(":")
        if not self.remote:
            raise DriveAuthError("RCLONE_REMOTE is required when GOOGLE_DRIVE_CLIENT=rclone")
        self.auth_mode = "rclone"
        self._source_config_path = config_path
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._runtime_config_path = config_path
        self._cache_dir: Path | None = None
        self._files_by_id: dict[str, DriveFile] = {}
        self._folder_paths_by_id: dict[str, str] = {}
        if config_path is not None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="kurukin-rclone-drive-")
            temp_root = Path(self._tempdir.name)
            self._runtime_config_path = temp_root / "rclone.conf"
            shutil.copy2(config_path, self._runtime_config_path)
            self._runtime_config_path.chmod(0o600)
            self._cache_dir = temp_root / "cache"
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls) -> "RcloneDriveClient":
        settings = get_settings()
        config_path = Path(settings.rclone_config) if settings.rclone_config else rclone_config_path()
        return cls(settings.rclone_remote or "", config_path)

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        payload = self._lsjson(["--drive-root-folder-id", folder_id, "--files-only"])
        files = [
            self._drive_file_from_lsjson_item(item, parent_id=folder_id)
            for item in payload[:limit]
            if not item.get("IsDir", False)
        ]
        for drive_file in files:
            self._files_by_id[drive_file.id] = drive_file
        return files

    def get_file_metadata(self, file_id: str) -> DriveFile:
        if file_id in self._files_by_id:
            return self._files_by_id[file_id]
        stat = self._lsjson(["--drive-root-folder-id", file_id, "--stat"])
        if isinstance(stat, dict) and stat.get("IsDir"):
            return DriveFile(
                id=file_id,
                name=str(stat.get("Name") or ""),
                mime_type=DRIVE_FOLDER_MIME,
                parents=[],
                size=None,
                modified_time=parse_rclone_mod_time(stat.get("ModTime")),
                capabilities={
                    "canAddChildren": True,
                    "canEdit": True,
                    "canMoveItemWithinDrive": True,
                    "canUntrash": True,
                },
            )
        raise DriveFileNotFoundError(f"file metadata is not cached for ID {file_id}")

    def download_file(self, file_id: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["rclone", "backend", "copyid", self.remote_path(), file_id, str(local_path)], timeout=600)

    def create_folder(self, parent_id: str, name: str) -> str:
        clean_name = str(PurePosixPath(name).name)
        self._run(
            ["rclone", "mkdir", f"{self.remote}:{clean_name}", "--drive-root-folder-id", parent_id],
            timeout=120,
        )
        for drive_file in self._lsjson(["--drive-root-folder-id", parent_id]):
            if drive_file.get("IsDir") and drive_file.get("Name") == clean_name and drive_file.get("ID"):
                folder_id = str(drive_file["ID"])
                parent_path = self._folder_paths_by_id.get(parent_id, "")
                self._folder_paths_by_id[folder_id] = str(PurePosixPath(parent_path) / clean_name)
                return folder_id
        raise ManagedDriveError("rclone mkdir succeeded but created folder ID was not found")

    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile:
        raise MoveError("rclone is read-only for managed Drive mutations")

    def restore_file_location(self, file_id: str, original_name: str, original_parent_id: str) -> DriveFile:
        raise MoveError("rclone is read-only for managed Drive mutations")

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        for drive_file in self.list_folder(expected_parent_id, 1000):
            if drive_file.id == file_id:
                return drive_file.name == expected_name
        return False

    def folder_exists(self, folder_id: str) -> bool:
        try:
            return self.get_file_metadata(folder_id).is_folder
        except ManagedDriveError:
            return False

    def remote_path(self) -> str:
        return f"{self.remote}:"

    def _lsjson(self, args: list[str]) -> Any:
        result = self._run(["rclone", "lsjson", self.remote_path(), *args], timeout=120)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ManagedDriveError("rclone lsjson returned invalid JSON") from exc

    def _run(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        prepared = list(command)
        if self._runtime_config_path is not None:
            prepared[1:1] = ["--config", str(self._runtime_config_path)]
        if self._cache_dir is not None:
            prepared[1:1] = ["--cache-dir", str(self._cache_dir)]
        env = os.environ.copy()
        if self._runtime_config_path is not None:
            env["RCLONE_CONFIG"] = str(self._runtime_config_path)
        if self._cache_dir is not None:
            env["XDG_CACHE_HOME"] = str(self._cache_dir)
        try:
            return subprocess.run(
                prepared,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            detail = sanitize_rclone_message((exc.stderr or exc.stdout or str(exc)).strip())
            raise ManagedDriveError(detail) from exc
        except subprocess.TimeoutExpired as exc:
            raise ManagedDriveError(f"rclone command timed out after {timeout}s") from exc

    def _drive_file_from_lsjson_item(self, item: dict[str, Any], parent_id: str) -> DriveFile:
        mime_type = item.get("MimeType")
        if item.get("IsDir"):
            mime_type = DRIVE_FOLDER_MIME
        drive_file_id = str(item.get("ID") or "")
        if not drive_file_id:
            raise DriveFileNotFoundError(f"rclone lsjson item has no ID: {item.get('Name') or item.get('Path')}")
        return DriveFile(
            id=drive_file_id,
            name=str(item.get("Name") or Path(str(item.get("Path") or "")).name),
            mime_type=mime_type,
            parents=[parent_id],
            size=int(item["Size"]) if item.get("Size") not in (None, -1, "") else None,
            modified_time=parse_rclone_mod_time(item.get("ModTime")),
            md5_checksum=rclone_hash(item),
            capabilities={"canEdit": True, "canMoveItemWithinDrive": True, "canUntrash": True},
        )


class GoogleDriveMutationClient:
    auth_mode = "drive_api_v3_rclone_oauth"

    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def from_rclone_remote(cls, remote: str, config_path: Path | None = None) -> "GoogleDriveMutationClient":
        config_path = config_path or rclone_config_path()
        if config_path is None or not config_path.exists():
            raise DriveAuthError("rclone.conf is missing")
        parser = configparser.ConfigParser()
        parser.read(config_path)
        remote_name = remote.strip().rstrip(":")
        if remote_name not in parser:
            raise DriveAuthError(f"rclone remote is missing: {remote_name}")
        section = parser[remote_name]
        missing = []
        token_blob = section.get("token")
        client_id = section.get("client_id")
        client_secret = section.get("client_secret")
        token_uri = section.get("token_url") or "https://oauth2.googleapis.com/token"
        if not token_blob:
            missing.append("token")
        if not client_id:
            missing.append("client_id")
        if not client_secret:
            missing.append("client_secret")
        if not token_uri:
            missing.append("token_uri")
        if missing:
            raise DriveAuthError(f"rclone remote lacks required OAuth fields: {', '.join(missing)}")
        try:
            token = json.loads(token_blob or "{}")
        except json.JSONDecodeError as exc:
            raise DriveAuthError("rclone token is not valid JSON") from exc
        token_missing = [name for name in ("access_token", "refresh_token") if not token.get(name)]
        if token_missing:
            raise DriveAuthError(f"rclone token lacks required OAuth fields: {', '.join(token_missing)}")
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveAuthError("google-api-python-client and google-auth are required") from exc
        credentials = Credentials(
            token=token["access_token"],
            refresh_token=token["refresh_token"],
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[DRIVE_SCOPE],
        )
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False))

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        query = f"'{folder_id}' in parents and trashed = false"
        payload = self.service.files().list(
            q=query,
            pageSize=limit,
            fields=file_fields("files"),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="createdTime",
        ).execute()
        return [drive_file_from_payload(item) for item in payload.get("files", [])]

    def get_file_metadata(self, file_id: str) -> DriveFile:
        payload = self.service.files().get(
            fileId=file_id,
            supportsAllDrives=True,
            fields=file_fields(),
        ).execute()
        return drive_file_from_payload(payload)

    def download_file(self, file_id: str, local_path: Path) -> None:
        raise MoveError("GoogleDriveMutationClient is not used for downloads")

    def create_folder(self, parent_id: str, name: str) -> str:
        existing = self._find_folder(parent_id, name)
        if existing:
            return existing
        payload = self.service.files().create(
            body={"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [parent_id]},
            supportsAllDrives=True,
            fields="id",
        ).execute()
        return str(payload["id"])

    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile:
        add_parents = new_parent_id if original_parent_id else None
        remove_parents = original_parent_id or None
        payload = self.service.files().update(
            fileId=file_id,
            body={"name": new_name, "trashed": False},
            addParents=add_parents,
            removeParents=remove_parents,
            supportsAllDrives=True,
            fields="id,name,parents,trashed,explicitlyTrashed,size,mimeType,modifiedTime,driveId",
        ).execute()
        return drive_file_from_payload(payload)

    def restore_file_location(self, file_id: str, original_name: str, original_parent_id: str) -> DriveFile:
        metadata = self.get_file_metadata(file_id)
        remove_parents = ",".join(parent for parent in metadata.parents if parent != original_parent_id)
        payload = self.service.files().update(
            fileId=file_id,
            body={"name": original_name, "trashed": False},
            addParents=original_parent_id,
            removeParents=remove_parents,
            supportsAllDrives=True,
            fields="id,name,parents,trashed,explicitlyTrashed,size,mimeType,modifiedTime,driveId",
        ).execute()
        return drive_file_from_payload(payload)

    def trash_file(self, file_id: str) -> DriveFile:
        payload = self.service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
            fields=file_fields(),
        ).execute()
        return drive_file_from_payload(payload)

    def untrash_file(self, file_id: str) -> DriveFile:
        payload = self.service.files().update(
            fileId=file_id,
            body={"trashed": False},
            supportsAllDrives=True,
            fields=file_fields(),
        ).execute()
        return drive_file_from_payload(payload)

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        metadata = self.get_file_metadata(file_id)
        return (
            metadata.id == file_id
            and metadata.name == expected_name
            and expected_parent_id in metadata.parents
            and not metadata.trashed
        )

    def folder_exists(self, folder_id: str) -> bool:
        try:
            return self.get_file_metadata(folder_id).is_folder
        except ManagedDriveError:
            return False

    def trash_folder(self, folder_id: str) -> None:
        metadata = self.get_file_metadata(folder_id)
        if not metadata.is_folder:
            raise MoveError("cleanup target is not a folder")
        self.service.files().update(
            fileId=folder_id,
            body={"trashed": True},
            supportsAllDrives=True,
            fields="id,trashed",
        ).execute()

    def _find_folder(self, parent_id: str, name: str) -> str | None:
        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and name = '{escaped_name}' "
            f"and mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false"
        )
        payload = self.service.files().list(
            q=query,
            pageSize=1,
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = payload.get("files") or []
        return str(files[0]["id"]) if files else None


class GoogleDriveAPIClient:
    base_url = "https://www.googleapis.com/drive/v3"
    upload_url = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, access_token: str | None = None, credentials: Any | None = None, auth_mode: str = "adc") -> None:
        self.access_token = access_token
        self.credentials = credentials
        self.auth_mode = auth_mode

    @classmethod
    def from_environment(cls) -> "GoogleDriveAPIClient":
        settings = get_settings()
        auth_mode = settings.google_drive_auth_mode.strip().lower()
        if auth_mode == "access_token":
            token = (settings.google_drive_access_token or "").strip()
            if not token:
                raise DriveAuthError(
                    "GOOGLE_DRIVE_AUTH_MODE=access_token requires GOOGLE_DRIVE_ACCESS_TOKEN; "
                    "use GOOGLE_DRIVE_AUTH_MODE=adc for renewable credentials"
                )
            return cls(access_token=token, auth_mode=auth_mode)
        if auth_mode != "adc":
            raise DriveAuthError("GOOGLE_DRIVE_AUTH_MODE must be adc or access_token")
        credentials = load_adc_credentials()
        return cls(credentials=credentials, auth_mode=auth_mode)

    def list_folder(self, folder_id: str, limit: int) -> list[DriveFile]:
        query = f"'{folder_id}' in parents and trashed = false"
        payload = self._json(
            "GET",
            "/files?"
            + parse.urlencode(
                {
                    "q": query,
                    "pageSize": str(limit),
                    "fields": file_fields("files"),
                    "orderBy": "createdTime",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
            ),
        )
        return [drive_file_from_payload(item) for item in payload.get("files", [])]

    def get_file_metadata(self, file_id: str) -> DriveFile:
        try:
            payload = self._json(
                "GET",
                f"/files/{parse.quote(file_id)}?"
                + parse.urlencode({"fields": file_fields(), "supportsAllDrives": "true"}),
            )
        except ManagedDriveError as exc:
            raise DriveFileNotFoundError(str(exc)) from exc
        return drive_file_from_payload(payload)

    def download_file(self, file_id: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/files/{parse.quote(file_id)}?alt=media"
        req = request.Request(url, headers=self._headers())
        with request.urlopen(req, timeout=300) as response, local_path.open("wb") as output:
            shutil.copyfileobj(response, output)

    def create_folder(self, parent_id: str, name: str) -> str:
        existing = self._find_folder(parent_id, name)
        if existing:
            return existing
        payload = self._json(
            "POST",
            "/files?fields=id",
            {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
        )
        return str(payload["id"])

    def rename_and_move_file(
        self,
        file_id: str,
        new_name: str,
        new_parent_id: str,
        original_parent_id: str | None = None,
    ) -> DriveFile:
        metadata = self.get_file_metadata(file_id)
        old_parents = original_parent_id or ",".join(metadata.parents)
        query = parse.urlencode(
            {
                "addParents": new_parent_id,
                "removeParents": old_parents,
                "fields": file_fields(),
                "supportsAllDrives": "true",
            }
        )
        payload = self._json(
            "PATCH",
            f"/files/{parse.quote(file_id)}?{query}",
            {"name": new_name, "trashed": False},
        )
        return drive_file_from_payload(payload)

    def restore_file_location(self, file_id: str, original_name: str, original_parent_id: str) -> DriveFile:
        metadata = self.get_file_metadata(file_id)
        old_parents = ",".join(metadata.parents)
        query = parse.urlencode(
            {
                "addParents": original_parent_id,
                "removeParents": old_parents,
                "fields": file_fields(),
                "supportsAllDrives": "true",
            }
        )
        payload = self._json(
            "PATCH",
            f"/files/{parse.quote(file_id)}?{query}",
            {"name": original_name, "trashed": False},
        )
        return drive_file_from_payload(payload)

    def trash_file(self, file_id: str) -> DriveFile:
        payload = self._json(
            "PATCH",
            f"/files/{parse.quote(file_id)}?"
            + parse.urlencode({"fields": file_fields(), "supportsAllDrives": "true"}),
            {"trashed": True},
        )
        return drive_file_from_payload(payload)

    def untrash_file(self, file_id: str) -> DriveFile:
        payload = self._json(
            "PATCH",
            f"/files/{parse.quote(file_id)}?"
            + parse.urlencode({"fields": file_fields(), "supportsAllDrives": "true"}),
            {"trashed": False},
        )
        return drive_file_from_payload(payload)

    def verify_file_location(self, file_id: str, expected_name: str, expected_parent_id: str) -> bool:
        metadata = self.get_file_metadata(file_id)
        return (
            metadata.id == file_id
            and metadata.name == expected_name
            and expected_parent_id in metadata.parents
            and not metadata.trashed
        )

    def folder_exists(self, folder_id: str) -> bool:
        try:
            metadata = self.get_file_metadata(folder_id)
        except ManagedDriveError:
            return False
        return metadata.mime_type == "application/vnd.google-apps.folder"

    def trash_folder(self, folder_id: str) -> None:
        metadata = self.get_file_metadata(folder_id)
        if not metadata.is_folder:
            raise MoveError("cleanup target is not a folder")
        self._json("PATCH", f"/files/{parse.quote(folder_id)}?supportsAllDrives=true", {"trashed": True})

    def _find_folder(self, parent_id: str, name: str) -> str | None:
        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and name = '{escaped_name}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        payload = self._json(
            "GET",
            "/files?"
            + parse.urlencode(
                {
                    "q": query,
                    "pageSize": "1",
                    "fields": "files(id)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
            ),
        )
        files = payload.get("files") or []
        if files:
            return str(files[0]["id"])
        return None

    def _headers(self) -> dict[str, str]:
        if self.auth_mode == "adc":
            if self.credentials is None:
                raise DriveAuthError("ADC credentials are not initialized")
            if not self.credentials.valid or self.credentials.expired:
                try:
                    from google.auth.transport.requests import Request as GoogleAuthRequest
                except ImportError as exc:
                    raise DriveAuthError("google-auth is required for GOOGLE_DRIVE_AUTH_MODE=adc") from exc
                self.credentials.refresh(GoogleAuthRequest())
            token = getattr(self.credentials, "token", None)
            if not token:
                raise DriveAuthError("ADC credentials did not provide an access token")
            return {"Authorization": f"Bearer {token}"}
        if not self.access_token:
            raise DriveAuthError("GOOGLE_DRIVE_ACCESS_TOKEN is required")
        return {"Authorization": f"Bearer {self.access_token}"}

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            if hasattr(exc, "read"):
                try:
                    message = sanitize_error_message(exc.read().decode("utf-8", errors="replace"))
                except Exception:
                    message = sanitize_error_message(str(exc))
            else:
                message = sanitize_error_message(str(exc))
            if "401" in message or "Unauthorized" in message:
                raise DriveAuthError(
                    "Google Drive authentication failed or expired; use GOOGLE_DRIVE_AUTH_MODE=adc "
                    "for renewable credentials"
                ) from exc
            raise ManagedDriveError(message) from exc


def load_adc_credentials() -> Any:
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except ImportError as exc:
        raise DriveAuthError("google-auth is required for GOOGLE_DRIVE_AUTH_MODE=adc") from exc
    try:
        credentials, _project = google.auth.default(scopes=[DRIVE_SCOPE])
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise DriveAuthError(sanitize_error_message(str(exc))) from exc
    return credentials


def file_fields(wrapper: str | None = None) -> str:
    fields = (
        "id,name,mimeType,parents,driveId,size,modifiedTime,md5Checksum,trashed,explicitlyTrashed,"
        "shortcutDetails,"
        "capabilities/canAddChildren,capabilities/canEdit,capabilities/canListChildren,"
        "capabilities/canModifyContent,capabilities/canMoveItemOutOfDrive,"
        "capabilities/canMoveItemWithinDrive,capabilities/canRename,capabilities/canTrash,"
        "capabilities/canUntrash"
    )
    return f"{wrapper}({fields})" if wrapper else fields


def parse_rclone_mod_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def rclone_hash(item: dict[str, Any]) -> str | None:
    hashes = item.get("Hashes")
    if not isinstance(hashes, dict):
        return None
    for key in ("md5", "MD5"):
        value = hashes.get(key)
        if value:
            return str(value)
    return None


def drive_file_from_payload(payload: dict[str, Any]) -> DriveFile:
    modified = payload.get("modifiedTime")
    modified_time = None
    if isinstance(modified, str):
        modified_time = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    size = payload.get("size")
    return DriveFile(
        id=str(payload["id"]),
        name=str(payload["name"]),
        mime_type=payload.get("mimeType"),
        parents=[str(parent) for parent in payload.get("parents", [])],
        drive_id=payload.get("driveId"),
        size=int(size) if size not in (None, "") else None,
        modified_time=modified_time,
        md5_checksum=payload.get("md5Checksum"),
        trashed=bool(payload.get("trashed", False)),
        explicitly_trashed=payload.get("explicitlyTrashed"),
        capabilities={
            str(key): value if isinstance(value, bool) else None
            for key, value in (payload.get("capabilities") or {}).items()
        },
    )
