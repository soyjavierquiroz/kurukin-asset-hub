from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.asset import (
    BEST_SCENE_ROLE_VALUES,
    CAMERA_MOTION_VALUES,
    OVERLAY_SAFE_AREA_VALUES,
    PACING_VALUES,
    SHOT_TYPE_VALUES,
    SUBJECT_POSITION_VALUES,
    VISUAL_ENERGY_VALUES,
)
from app.services.people_metadata import (
    PersonVisibility,
    SourcePresentationHint,
    VisualPresentation,
    gendered_metadata_allowed,
    neutralize_gendered_terms,
    people_search_terms,
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
    title_es: str | None = None
    visual_description: str | None = None
    description_es: str | None = None
    action_description: str | None = None
    primary_theme: str | None = None
    primary_topic: str | None = None
    subject: str | None = None
    action: str | None = None
    context: str | None = None
    suggested_uses: list[str] = Field(default_factory=list)
    contains_people: bool = False
    people_count: int | None = Field(default=None, ge=0)
    visual_presentation: VisualPresentation = "not_applicable"
    visual_presentation_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    person_visibility: PersonVisibility = "not_applicable"
    search_terms: list[str] = Field(default_factory=list)
    source_presentation_hint: SourcePresentationHint | None = None
    can_flip_horizontal: bool = True
    flip_risk_reasons: list[str] = Field(default_factory=list)
    can_zoom: bool = True
    max_safe_zoom: float = 1.0
    generic_compatibility: bool = False
    safe_text_areas: list[str] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def normalize_people_metadata(self) -> "AIAssetEnrichmentResult":
        if not self.contains_people:
            self.people_count = None
            self.visual_presentation = "not_applicable"
            self.person_visibility = "not_applicable"
        if (
            self.visual_presentation in {"masculine", "feminine"}
            and self.person_visibility in {"back_view", "silhouette", "occluded"}
            and not gendered_metadata_allowed(
                self.visual_presentation_confidence,
                self.person_visibility,
            )
        ):
            self.visual_presentation = "unclear"
        self.search_terms = people_search_terms(
            self.visual_presentation,
            self.person_visibility,
            self.search_terms,
        )
        if self.contains_people and not gendered_metadata_allowed(
            self.visual_presentation_confidence,
            self.person_visibility,
        ):
            self.title = neutralize_gendered_terms(self.title)
            self.title_es = neutralize_gendered_terms(self.title_es)
            self.primary_topic = neutralize_gendered_terms(self.primary_topic)
        return self


def build_openai_strict_json_schema(model_class: type[BaseModel]) -> dict[str, Any]:
    schema = model_class.model_json_schema()
    _normalize_openai_strict_json_schema(schema)
    return schema


def _normalize_openai_strict_json_schema(schema: Any) -> None:
    if not isinstance(schema, dict):
        return

    schema.pop("default", None)

    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties.keys())
        schema["additionalProperties"] = False
        for property_schema in properties.values():
            _normalize_openai_strict_json_schema(property_schema)

    items = schema.get("items")
    if isinstance(items, dict):
        _normalize_openai_strict_json_schema(items)
    elif isinstance(items, list):
        for item_schema in items:
            _normalize_openai_strict_json_schema(item_schema)

    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant_schema in variants:
                _normalize_openai_strict_json_schema(variant_schema)

    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for def_schema in defs.values():
            _normalize_openai_strict_json_schema(def_schema)
