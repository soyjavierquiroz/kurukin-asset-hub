from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import (
    BEST_SCENE_ROLE_VALUES,
    CAMERA_MOTION_VALUES,
    OVERLAY_SAFE_AREA_VALUES,
    PACING_VALUES,
    SHOT_TYPE_VALUES,
    SUBJECT_POSITION_VALUES,
    VISUAL_ENERGY_VALUES,
)

KeywordCategory = Literal[
    "subject",
    "object",
    "action",
    "location",
    "mood",
    "style",
    "color",
    "concept",
    "culture",
    "scene_role",
    "usage",
    "restriction",
    "other",
]


class AIKeyword(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=160)
    category: KeywordCategory
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    confidence: float = Field(ge=0.0, le=1.0)
    language: str | None = Field(default=None, max_length=16)


class AIAssetEnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    visual_description: str | None = None
    action_description: str | None = None
    emotion: str | None = None
    people: str | None = None
    location: str | None = None
    best_for: str | None = None
    avoid_for: str | None = None
    negative_keywords: str | None = None
    shot_type: Literal[*SHOT_TYPE_VALUES]
    camera_motion: Literal[*CAMERA_MOTION_VALUES]
    subject_position: Literal[*SUBJECT_POSITION_VALUES]
    visual_energy: Literal[*VISUAL_ENERGY_VALUES]
    pacing: Literal[*PACING_VALUES]
    best_scene_role: Literal[*BEST_SCENE_ROLE_VALUES]
    has_visible_text: bool
    visible_text: str | None = None
    text_language: str | None = None
    has_logo: bool
    has_watermark: bool
    has_faces: bool
    has_hands: bool
    has_product: bool
    has_directional_motion: bool
    safe_for_subtitles: bool
    safe_for_text_overlay: bool
    overlay_safe_area: Literal[*OVERLAY_SAFE_AREA_VALUES]
    flip_horizontal_allowed: bool
    flip_vertical_allowed: bool
    crop_allowed: bool
    zoom_allowed: bool
    speed_change_allowed: bool
    reverse_allowed: bool
    color_grade_allowed: bool
    loopable: bool
    similarity_group: str | None = None
    keywords: list[AIKeyword] = Field(default_factory=list)
    search_text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool
    review_reason: str | None = None
