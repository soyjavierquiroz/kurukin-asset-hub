from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VISUAL_ANALYSIS_TYPE = "visual_intelligence"
VISUAL_PROFILE_VERSION = "visual-v1"
VISUAL_FRAME_COUNT = 10

QualityLabel = Literal["excellent", "good", "usable", "weak", "bad"]
SafeArea = Literal["top", "middle", "bottom", "left", "right", "none"]
PanDirection = Literal["left", "right", "up", "down"]
AspectRatio = Literal["9:16", "16:9", "1:1", "4:5"]


class VisualQualitySignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    label: QualityLabel
    sharpness: float = Field(ge=0.0, le=1.0)
    exposure: float = Field(ge=0.0, le=1.0)
    stability: float = Field(ge=0.0, le=1.0)
    lighting: float = Field(ge=0.0, le=1.0)
    temporal_consistency: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)


class VisualGarbageSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_garbage: bool
    score: float = Field(ge=0.0, le=1.0)
    black_or_blank: bool = False
    severe_blur: bool = False
    corrupted_frames: bool = False
    accidental_capture: bool = False
    reasons: list[str] = Field(default_factory=list)


class VisualSemanticSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_es: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    setting: str | None = None
    mood: str | None = None
    keywords_es: list[str] = Field(default_factory=list)
    contains_people: bool = False
    visible_text: str | None = None
    logo_or_watermark: bool = False


class VisualCompositionSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_type: str
    subject_position: str
    subject_framing: str
    vertical_suitability: float = Field(ge=0.0, le=1.0)
    horizontal_suitability: float = Field(ge=0.0, le=1.0)
    safe_text_areas: list[SafeArea] = Field(default_factory=list)
    crop_risk_reasons: list[str] = Field(default_factory=list)


class VisualFlipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    risk_reasons: list[str] = Field(default_factory=list)


class VisualZoomDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    max_safe_zoom: float = Field(ge=1.0, le=3.0)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualPanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    safe_directions: list[PanDirection] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualCropDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    preferred_aspect_ratios: list[AspectRatio] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualTransformDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flip: VisualFlipDecision
    zoom: VisualZoomDecision
    pan: VisualPanDecision
    crop: VisualCropDecision


class VisualIntelligenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality: VisualQualitySignals
    garbage: VisualGarbageSignals
    semantics: VisualSemanticSignals
    composition: VisualCompositionSignals
    transforms: VisualTransformDecisions
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool = False
    review_reason: str | None = None

    @field_validator(
        "quality",
        "garbage",
        "semantics",
        "composition",
        "transforms",
        mode="after",
    )
    @classmethod
    def normalize_nested_codes(cls, value):
        for attr in ("reason_codes", "reasons", "crop_risk_reasons", "risk_reasons"):
            items = getattr(value, attr, None)
            if isinstance(items, list):
                setattr(value, attr, normalize_codes(items))
        return value


def normalize_codes(values: list[str]) -> list[str]:
    codes: list[str] = []
    for value in values:
        code = str(value or "").strip().lower().replace(" ", "_")
        if code and code not in codes:
            codes.append(code[:80])
    return codes
