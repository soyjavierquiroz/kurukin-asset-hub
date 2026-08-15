from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


EDITORIAL_QUALITY_PROMPT_VERSION = "editorial_quality"
QUALITY_PROFILE_VERSION = "quality-v1"


class EditorialQualityVLMResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    editorial_quality_score: float = Field(ge=0.0, le=1.0)
    vertical_suitability_score: float = Field(ge=0.0, le=1.0)
    horizontal_suitability_score: float = Field(ge=0.0, le=1.0)
    subject_severely_out_of_frame: bool = False
    subject_badly_clipped: bool = False
    social_media_ui: bool = False
    subscribe_cta: bool = False
    emoji_overlay: bool = False
    watermark: bool = False
    heavy_text_overlay: bool = False
    nearly_empty: bool = False
    severe_blur: bool = False
    severe_black_frames: bool = False
    editorial_usable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("reason_codes")
    @classmethod
    def normalize_reason_codes(cls, value: list[str]) -> list[str]:
        codes: list[str] = []
        for item in value:
            code = str(item or "").strip().lower().replace(" ", "_")
            if code and code not in codes:
                codes.append(code[:80])
        return codes
