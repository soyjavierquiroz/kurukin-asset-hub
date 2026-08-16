from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VISUAL_ANALYSIS_TYPE = "visual_intelligence"
VISUAL_PROFILE_VERSION = "visual-v1"
VISUAL_FRAME_COUNT = 10

QualityLabel = Literal["excellent", "good", "usable", "weak", "bad"]
SafeArea = Literal["top", "middle", "bottom", "left", "right", "center", "none"]
PanDirection = Literal["left", "right", "up", "down"]
AspectRatio = Literal["9:16", "16:9", "1:1", "4:5"]
CameraMotion = Literal[
    "static",
    "pan_left",
    "pan_right",
    "tilt_up",
    "tilt_down",
    "zoom_in",
    "zoom_out",
    "handheld",
    "tracking",
    "high_motion",
    "unknown",
]


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
    black_or_blank: bool
    subject_severely_out_of_frame: bool
    subject_badly_clipped: bool
    social_media_ui: bool
    subscribe_cta: bool
    emoji_overlay: bool
    watermark: bool
    logo: bool
    heavy_text_overlay: bool
    nearly_empty: bool
    severe_blur: bool
    severe_black_frames: bool
    corrupted_frames: bool
    accidental_capture: bool
    editorial_usable: bool
    reasons: list[str]


class VisualSemanticSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_es: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    narrative_themes: list[str] = Field(default_factory=list)
    possible_use_cases: list[str] = Field(default_factory=list)
    negative_use_cases: list[str] = Field(default_factory=list)
    setting: str | None = None
    mood: str | None = None
    keywords_es: list[str] = Field(default_factory=list)
    contains_people: bool = False
    visible_text: str | None = None
    logo_or_watermark: bool = False


class VisualSubjectTrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: float = Field(ge=0.0)
    center: list[float] = Field(min_length=2, max_length=2)
    bbox: list[float] = Field(min_length=4, max_length=4)

    @field_validator("center", "bbox")
    @classmethod
    def normalized_geometry(cls, value: list[float]) -> list[float]:
        return [max(0.0, min(float(item), 1.0)) for item in value]


class VisualCompositionSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_type: str
    subject_position: str
    subject_framing: str
    subject_region: list[float] | None = Field(default=None, min_length=4, max_length=4)
    subject_trajectory: list[VisualSubjectTrajectoryPoint] = Field(default_factory=list)
    negative_space: float = Field(default=0.0, ge=0.0, le=1.0)
    edge_proximity: float = Field(default=0.0, ge=0.0, le=1.0)
    vertical_suitability: float = Field(ge=0.0, le=1.0)
    horizontal_suitability: float = Field(ge=0.0, le=1.0)
    camera_motion: CameraMotion = "unknown"
    camera_motion_confidence: float = Field(ge=0.0, le=1.0)
    safe_text_areas: list[SafeArea] = Field(default_factory=list)
    crop_risk_reasons: list[str] = Field(default_factory=list)

    @field_validator("subject_region")
    @classmethod
    def normalized_region(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        return [max(0.0, min(float(item), 1.0)) for item in value]


class VisualFlipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualZoomDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    max_safe_zoom: float = Field(ge=1.0, le=3.0)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualPanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    safe_directions: list[PanDirection] = Field(default_factory=list)
    max_offset_x: float | None = Field(default=None, ge=0.0, le=1.0)
    max_offset_y: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_reasons: list[str] = Field(default_factory=list)


class VisualCropDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    safe_rect: list[float] | None = Field(default=None, min_length=4, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)
    preferred_aspect_ratios: list[AspectRatio] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)

    @field_validator("safe_rect")
    @classmethod
    def normalized_safe_rect(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        return [max(0.0, min(float(item), 1.0)) for item in value]


class VisualTransformDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flip: VisualFlipDecision
    zoom: VisualZoomDecision
    pan: VisualPanDecision
    crop_vertical: VisualCropDecision
    crop_horizontal: VisualCropDecision
    crop: VisualCropDecision | None = None


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
