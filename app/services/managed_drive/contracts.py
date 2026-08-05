from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

VisualPresentation = Literal["masculine", "feminine", "mixed", "unclear", "not_applicable"]
PersonVisibility = Literal["clear", "partial", "back_view", "silhouette", "occluded", "not_applicable"]
SourcePresentationHint = Literal["masculine", "feminine", "unclear"]


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str | None = None
    parents: list[str] = field(default_factory=list)
    drive_id: str | None = None
    size: int | None = None
    modified_time: datetime | None = None
    md5_checksum: str | None = None
    capabilities: dict[str, bool | None] = field(default_factory=dict)
    trashed: bool = False
    explicitly_trashed: bool | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == DRIVE_FOLDER_MIME


@dataclass(frozen=True)
class PreflightRequest:
    source_folder_id: str
    destination_root_id: str
    file_id: str | None = None


@dataclass(frozen=True)
class IngestRequest:
    source_folder_id: str
    destination_root_id: str
    scope: str
    limit: int = 10
    apply: bool = False
    dry_run: bool = True
    file_id: str | None = None
    brand_slug: str | None = None
    collection: str = "evergreen"
    title_type: str | None = None
    title: str | None = None
    season: int | None = None
    episode: int | None = None
    replan: bool = False
    simulate_drive_mutation: bool = False


@dataclass(frozen=True)
class TechnicalResult:
    mime_type: str
    media_type: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    fps: float | None = None
    codec: str | None = None
    has_audio: bool | None = None


@dataclass(frozen=True)
class PreviewResult:
    thumbnail_path: str | None
    preview_path: str | None
    warnings: list[str] = field(default_factory=list)


class ManagedAIResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title_es: str | None = None
    description_es: str | None = None
    primary_theme: str | None = None
    primary_topic: str | None = None
    subject: str | None = None
    action: str | None = None
    context: str | None = None
    tags: list[str] = Field(default_factory=list)
    tags_source: str | None = None
    suggested_uses: list[str] = Field(default_factory=list)
    contains_people: bool = False
    people_count: int | None = None
    visual_presentation: VisualPresentation = "not_applicable"
    visual_presentation_confidence: float = 0.0
    person_visibility: PersonVisibility = "not_applicable"
    search_terms: list[str] = Field(default_factory=list)
    source_presentation_hint: SourcePresentationHint | None = None
    can_flip_horizontal: bool = True
    flip_risk_reasons: list[str] = Field(default_factory=list)
    can_zoom: bool = True
    max_safe_zoom: float = 1.0
    has_visible_text: bool = False
    has_logo: bool = False
    generic_compatibility: bool = False
    camera_motion: str = "unknown"
    shot_type: str = "unknown"
    subject_position: str = "unknown"
    safe_text_areas: list[str] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    requires_review: bool = False
    warnings: list[str] = Field(default_factory=list)
    ai_analysis_mode: str = "FALLBACK"
    ai_provider: str | None = None
    ai_model: str | None = None
    frames_analyzed: int = 0


@dataclass(frozen=True)
class Classification:
    primary_theme: str
    primary_topic: str
    ambiguous: bool = False


@dataclass(frozen=True)
class BatchScope:
    scope: str
    source_folder_id: str
    destination_root_id: str
    brand_slug: str | None = None
    collection: str = "evergreen"
    title_type: str | None = None
    title: str | None = None
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True)
class BatchRequest:
    apply: bool = False
    max_total: int = 10
    max_per_scope: int = 5
    concurrency: int = 1
    nvidia_pause_seconds: float = 0.75
    only_file_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewApproval:
    file_id: str
    primary_theme: str | None = None
    primary_topic: str | None = None
    tags: tuple[str, ...] = ()
    reviewed_by: str | None = None
    visual_presentation: VisualPresentation | None = None
    visual_presentation_confidence: float | None = None
    person_visibility: PersonVisibility | None = None
    people_count: int | None = None


__all__ = [
    "BatchRequest",
    "BatchScope",
    "Classification",
    "DriveFile",
    "IngestRequest",
    "ManagedAIResult",
    "PreflightRequest",
    "PreviewResult",
    "ReviewApproval",
    "TechnicalResult",
]
