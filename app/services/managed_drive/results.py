from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MutationStats:
    attempted: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    uncompensated: list[str] = field(default_factory=list)

    def attempt(self, event: str) -> None:
        self.attempted.append(event)

    def succeed(self, event: str) -> None:
        self.succeeded.append(event)

    def compensate(self, event: str) -> None:
        self.compensated.append(event)

    def uncompensate(self, event: str) -> None:
        self.uncompensated.append(event)

    def to_dict(self) -> dict[str, int]:
        return {
            "mutations_attempted": len(self.attempted),
            "mutations_succeeded": len(self.succeeded),
            "mutations_compensated": len(self.compensated),
            "mutations_uncompensated": len(self.uncompensated),
        }


@dataclass(frozen=True)
class PreflightResult:
    auth_mode: str
    source_folder: str
    source_drive: str
    destination_folder: str
    destination_drive: str
    same_drive: bool
    can_list_source: bool
    can_create_in_destination: bool
    can_move_files: bool
    test_file_id: str | None
    destination_inside_pilot_root: bool
    legacy_ids_detected: list[str]
    scopes_complete: bool
    warnings: list[str] = field(default_factory=list)
    drive_mutations_performed: int = 0
    auth_refreshable: bool = False
    custom_oauth_client: bool = False
    source_accessible: bool = False
    root_accessible: bool = False
    file_accessible: bool = False
    file_trashed: bool | None = None
    can_update: bool = False
    can_move_within_drive: bool = False
    can_add_children: bool = False
    can_restore_from_trash: bool = False
    oauth_scope_sufficient: bool = False

    @property
    def ready_for_dry_run(self) -> bool:
        return (
            self.source_folder == "OK"
            and self.destination_folder == "OK"
            and self.same_drive
            and self.can_list_source
            and self.can_create_in_destination
            and self.can_move_files
            and self.destination_inside_pilot_root
            and not self.legacy_ids_detected
            and self.scopes_complete
            and self.drive_mutations_performed == 0
        )

    def to_lines(self) -> list[str]:
        return [
            "AUTHENTICATION: OK",
            f"AUTH_MODE: {self.auth_mode}",
            f"SOURCE_FOLDER: {self.source_folder}",
            f"SOURCE_DRIVE: {self.source_drive}",
            f"DESTINATION_FOLDER: {self.destination_folder}",
            f"DESTINATION_DRIVE: {self.destination_drive}",
            f"SAME_DRIVE: {'YES' if self.same_drive else 'NO'}",
            f"CAN_LIST_SOURCE: {'YES' if self.can_list_source else 'NO'}",
            f"CAN_CREATE_IN_DESTINATION: {'YES' if self.can_create_in_destination else 'NO'}",
            f"CAN_MOVE_FILES: {'YES' if self.can_move_files else 'NO'}",
            f"TEST_FILE_ID: {self.test_file_id or 'NONE'}",
            f"DESTINATION_INSIDE_PILOT_ROOT: {'YES' if self.destination_inside_pilot_root else 'NO'}",
            f"LEGACY_IDS_DETECTED: {','.join(self.legacy_ids_detected) if self.legacy_ids_detected else 'NO'}",
            f"SCOPES_COMPLETE: {'YES' if self.scopes_complete else 'NO'}",
            f"DRIVE_MUTATIONS_PERFORMED: {self.drive_mutations_performed}",
            f"READY_FOR_DRY_RUN: {'YES' if self.ready_for_dry_run else 'NO'}",
            f"WARNINGS: {','.join(self.warnings) if self.warnings else 'NONE'}",
            f"AUTH_REFRESHABLE={'YES' if self.auth_refreshable else 'NO'}",
            f"CUSTOM_OAUTH_CLIENT={'YES' if self.custom_oauth_client else 'NO'}",
            f"SOURCE_ACCESSIBLE={'YES' if self.source_accessible else 'NO'}",
            f"ROOT_ACCESSIBLE={'YES' if self.root_accessible else 'NO'}",
            f"FILE_ACCESSIBLE={'YES' if self.file_accessible else 'NO'}",
            f"FILE_TRASHED={'YES' if self.file_trashed else 'NO'}",
            f"CAN_UPDATE={'YES' if self.can_update else 'NO'}",
            f"CAN_MOVE_WITHIN_DRIVE={'YES' if self.can_move_within_drive else 'NO'}",
            f"CAN_ADD_CHILDREN={'YES' if self.can_add_children else 'NO'}",
            f"CAN_RESTORE_FROM_TRASH={'YES' if self.can_restore_from_trash else 'NO'}",
            f"OAUTH_SCOPE_SUFFICIENT={'YES' if self.oauth_scope_sufficient else 'NO'}",
        ]


@dataclass(frozen=True)
class IngestPlan:
    drive_file_id: str
    original_name: str
    original_parent_id: str | None
    source_drive_id: str | None
    scope: str
    mime_type: str | None
    media_type: str
    width: int | None
    height: int | None
    duration: float | None
    aspect_ratio: float | None
    orientation: str
    title_es: str | None
    description_es: str | None
    primary_theme: str
    primary_topic: str
    suggested_tags: list[str]
    suggested_uses: list[str]
    can_flip_horizontal: bool
    flip_risk_reasons: list[str]
    can_zoom: bool
    max_safe_zoom: float | None
    has_visible_text: bool
    has_logo: bool
    generic_compatibility: bool
    target_path: str
    target_name: str
    target_batch: int | None
    path_layout_version: str | None
    preview_path: str | None
    thumbnail_path: str | None
    classification_confidence: float | None
    warnings: list[str]
    tags_source: str | None = None
    requires_review: bool = False
    drive_mutations_performed: int = 0
    mutation_stats: dict[str, int] = field(default_factory=dict)
    simulated_update: dict[str, Any] | None = None
    plan_version: str | None = None
    plan_created_at: datetime | None = None
    plan_hash: str | None = None
    database_status: str = "planned"
    brand: str | None = None
    title: str | None = None
    collection: str | None = None
    asset_id: int | None = None
    status: str = "move_planned"
    move_status: str = "planned"
    ai_analysis_mode: str = "FALLBACK"
    ai_provider: str | None = None
    ai_model: str | None = None
    frames_analyzed: int = 0
    thumbnail_valid: bool = False
    thumbnail_size: int | None = None
    result: str = "planned"
    ai_call: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive_file_id": self.drive_file_id,
            "original_name": self.original_name,
            "original_parent_id": self.original_parent_id,
            "source_drive_id": self.source_drive_id,
            "scope": self.scope,
            "brand": self.brand,
            "title": self.title,
            "collection": self.collection,
            "mime_type": self.mime_type,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "aspect_ratio": self.aspect_ratio,
            "orientation": self.orientation,
            "title_es": self.title_es,
            "description_es": self.description_es,
            "primary_theme": self.primary_theme,
            "primary_topic": self.primary_topic,
            "tags": self.suggested_tags,
            "tags_source": self.tags_source,
            "suggested_uses": self.suggested_uses,
            "can_flip_horizontal": self.can_flip_horizontal,
            "flip_risk_reasons": self.flip_risk_reasons,
            "can_zoom": self.can_zoom,
            "max_safe_zoom": self.max_safe_zoom,
            "has_visible_text": self.has_visible_text,
            "has_logo": self.has_logo,
            "generic_compatibility": self.generic_compatibility,
            "target_path": self.target_path,
            "target_name": self.target_name,
            "target_batch": self.target_batch,
            "path_layout_version": self.path_layout_version,
            "preview_path": self.preview_path,
            "thumbnail_path": self.thumbnail_path,
            "classification_confidence": self.classification_confidence,
            "warnings": self.warnings,
            "requires_review": self.requires_review,
            "drive_mutations_performed": self.drive_mutations_performed,
            "mutation_stats": self.mutation_stats,
            "simulated_update": self.simulated_update,
            "plan_version": self.plan_version,
            "plan_created_at": self.plan_created_at.isoformat() if self.plan_created_at else None,
            "plan_hash": self.plan_hash,
            "database_status": self.database_status,
            "asset_id": self.asset_id,
            "status": self.status,
            "move_status": self.move_status,
            "ai_analysis_mode": self.ai_analysis_mode,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "frames_analyzed": self.frames_analyzed,
            "thumbnail_valid": self.thumbnail_valid,
            "thumbnail_size": self.thumbnail_size,
            "result": self.result,
            "ai_call": self.ai_call,
        }


@dataclass
class BatchAssetResult:
    drive_file_id: str
    scope: str
    original_name: str
    provider: str | None = None
    analysis_result: str = "skipped"
    target_path: str | None = None
    final_status: str | None = None
    move_status: str | None = None
    review_reason: str | None = None
    drive_mutations: int = 0
    error: str | None = None
    pending_analysis: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive_file_id": self.drive_file_id,
            "scope": self.scope,
            "original_name": self.original_name,
            "provider": self.provider,
            "analysis_result": self.analysis_result,
            "target_path": self.target_path,
            "final_status": self.final_status,
            "move_status": self.move_status,
            "review_reason": self.review_reason,
            "drive_mutations": self.drive_mutations,
            "error": self.error,
            "pending_analysis": self.pending_analysis,
        }


@dataclass
class BatchCounters:
    files_discovered: int = 0
    files_new: int = 0
    files_skipped: int = 0
    files_analyzed: int = 0
    files_applied: int = 0
    files_ready: int = 0
    files_sent_to_review: int = 0
    files_failed: int = 0
    nvidia_requests: int = 0
    nvidia_retries: int = 0
    openai_requests: int = 0
    drive_mutations_attempted: int = 0
    drive_mutations_succeeded: int = 0
    duplicates: int = 0
    pending_analysis: int = 0
    already_applied: int = 0


@dataclass
class BatchResult:
    run_mode: str
    started_at: datetime
    finished_at: datetime
    concurrency: int
    lock_acquired: bool
    counters: BatchCounters
    assets: list[BatchAssetResult] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return round((self.finished_at - self.started_at).total_seconds(), 3)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "run_mode": self.run_mode,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "concurrency": self.concurrency,
            "lock": "acquired" if self.lock_acquired else "blocked",
            "duration_seconds": self.duration_seconds,
        }
        data.update(self.counters.__dict__)
        data["assets"] = [asset.to_dict() for asset in self.assets]
        return data


@dataclass
class LegacyLayoutAssetPlan:
    drive_file_id: str
    status: str
    move_status: str
    scope: str
    current_name: str
    current_parent_id: str
    current_path: str
    target_name: str
    compact_target_path: str
    compact_target_parts: list[str]
    target_parent_id: str | None
    trashed: bool
    size: int | None
    mime_type: str | None
    name_changed: bool
    would_add_parent: str | None
    would_remove_parent: str
    would_set_trashed_false: bool
    database_updates: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive_file_id": self.drive_file_id,
            "status": self.status,
            "move_status": self.move_status,
            "scope": self.scope,
            "current_name": self.current_name,
            "current_parent_id": self.current_parent_id,
            "current_path": self.current_path,
            "target_name": self.target_name,
            "compact_target_path": self.compact_target_path,
            "target_parent_id": self.target_parent_id,
            "trashed": self.trashed,
            "size": self.size,
            "mimeType": self.mime_type,
            "name_changed": self.name_changed,
            "would_add_parent": self.would_add_parent,
            "would_remove_parent": self.would_remove_parent,
            "would_set_trashed_false": self.would_set_trashed_false,
            "database_updates": self.database_updates,
        }


@dataclass
class LegacyLayoutMigrationResult:
    simulated: bool
    assets_detected: list[str] = field(default_factory=list)
    assets_migrated: int = 0
    generic_migrated: int = 0
    brand_migrated: int = 0
    title_migrated: int = 0
    review_migrated: int = 0
    folders_created: int = 0
    folders_deleted: int = 0
    drive_mutations: int = 0
    duplicates: int = 0
    cleaned_folder_ids: list[str] = field(default_factory=list)
    not_deleted_folder_ids: list[str] = field(default_factory=list)
    managed_folder_records_deleted: int = 0
    plans: list[LegacyLayoutAssetPlan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulated": self.simulated,
            "assets_detected": self.assets_detected,
            "assets_migrated": self.assets_migrated,
            "generic_migrated": self.generic_migrated,
            "brand_migrated": self.brand_migrated,
            "title_migrated": self.title_migrated,
            "review_migrated": self.review_migrated,
            "folders_created": self.folders_created,
            "folders_deleted": self.folders_deleted,
            "drive_mutations": self.drive_mutations,
            "duplicates": self.duplicates,
            "cleaned_folder_ids": self.cleaned_folder_ids,
            "not_deleted_folder_ids": self.not_deleted_folder_ids,
            "managed_folder_records_deleted": self.managed_folder_records_deleted,
            "plans": [plan.to_dict() for plan in self.plans],
        }


@dataclass(frozen=True)
class DriveStatusRow:
    drive_file_id: str | None
    scope: str | None
    original_name: str | None
    current_name: str
    status: str
    move_status: str
    requires_review: bool
    review_reason: str | None
    provider: str | None
    model: str | None
    target_path: str | None
    path_layout_version: str | None
    updated_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive_file_id": self.drive_file_id,
            "scope": self.scope,
            "original_name": self.original_name,
            "current_name": self.current_name,
            "status": self.status,
            "move_status": self.move_status,
            "requires_review": self.requires_review,
            "review_reason": self.review_reason,
            "provider": self.provider,
            "model": self.model,
            "target_path": self.target_path,
            "path_layout_version": self.path_layout_version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True)
class DriveStatusResult:
    rows: list[DriveStatusRow]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "assets": [row.to_dict() for row in self.rows],
        }


__all__ = [
    "BatchAssetResult",
    "BatchCounters",
    "BatchResult",
    "DriveStatusResult",
    "DriveStatusRow",
    "IngestPlan",
    "LegacyLayoutAssetPlan",
    "LegacyLayoutMigrationResult",
    "MutationStats",
    "PreflightResult",
]
