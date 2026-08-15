from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings
from app.schemas.ai_enrichment import AIAssetEnrichmentResult, AIKeyword
from app.services.ai_providers.openai_provider import build_vision_chat_content, sanitize_provider_error
from app.services.people_metadata import PersonVisibility, VisualPresentation

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
ALLOWED_ZOOMS = {1.0, 1.1, 1.2}


class NvidiaProviderError(RuntimeError):
    pass


class NvidiaReducedAssetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_es: str = Field(min_length=1)
    description_es: str = Field(min_length=1)
    primary_theme: str
    primary_topic: str
    tags: list[str] = Field(default_factory=list)
    suggested_uses: list[str] = Field(default_factory=list)
    contains_people: bool = False
    people_count: int | None = Field(default=None, ge=0)
    visual_presentation: VisualPresentation = "not_applicable"
    visual_presentation_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    person_visibility: PersonVisibility = "not_applicable"
    search_terms: list[str] = Field(default_factory=list)
    has_visible_text: bool
    has_logo: bool
    can_flip_horizontal: bool
    flip_risk_reasons: list[str] = Field(default_factory=list)
    can_zoom: bool
    max_safe_zoom: float
    generic_compatibility: bool
    requires_review: bool
    warnings: list[str] = Field(default_factory=list)

    @field_validator("primary_theme")
    @classmethod
    def validate_primary_theme(cls, value: str) -> str:
        if value not in PRIMARY_THEMES:
            raise ValueError("primary_theme is outside the controlled taxonomy")
        return value

    @field_validator("primary_topic")
    @classmethod
    def validate_primary_topic(cls, value: str) -> str:
        topic = normalize_primary_topic(value)
        word_count = len(topic.split())
        if word_count < 1 or word_count > 8:
            raise ValueError("primary_topic must have 1 to 8 words")
        return topic


def call_nvidia_vision(
    prompt: str,
    image_paths: list[Path],
    model: str | None = None,
    timeout_seconds: float = 60.0,
) -> AIAssetEnrichmentResult:
    settings = get_settings()
    if not settings.nvidia_api_key:
        raise NvidiaProviderError("NVIDIA_API_KEY is not configured")

    response_text = post_chat_completion(
        build_reduced_prompt(prompt),
        image_paths,
        model or settings.nvidia_model,
        timeout_seconds,
    )
    reduced = parse_reduced_result(response_text)
    return reduced_to_asset_result(reduced)


def post_chat_completion(
    prompt: str,
    image_paths: list[Path],
    model: str,
    timeout_seconds: float,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
    provider_options: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_vision_chat_content(prompt, image_paths)}],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": max_tokens if max_tokens is not None else settings.nvidia_max_tokens,
        "stream": False,
    }
    if extra_body:
        payload.update(extra_body)
    if provider_options:
        payload.update(provider_options)
    request = urllib.request.Request(
        settings.nvidia_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + settings.nvidia_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", "replace")
        raise NvidiaProviderError(sanitize_provider_error(message)) from exc
    except Exception as exc:
        raise NvidiaProviderError(sanitize_provider_error(str(exc))) from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str) and content.strip():
            return content
    raise NvidiaProviderError("NVIDIA response did not include message content")


def build_reduced_prompt(base_prompt: str) -> str:
    return (
        "Analiza estos frames de un asset visual para una biblioteca de videos.\n"
        "Responde exclusivamente con un objeto JSON valido, sin markdown, comentarios ni explicaciones.\n\n"
        "Usa exactamente estas claves y tipos:\n"
        "{\n"
        '  "title_es": "",\n'
        '  "description_es": "",\n'
        '  "primary_theme": "",\n'
        '  "primary_topic": "",\n'
        '  "tags": [],\n'
        '  "suggested_uses": [],\n'
        '  "contains_people": false,\n'
        '  "people_count": null,\n'
        '  "visual_presentation": "not_applicable",\n'
        '  "visual_presentation_confidence": 0.0,\n'
        '  "person_visibility": "not_applicable",\n'
        '  "search_terms": [],\n'
        '  "has_visible_text": false,\n'
        '  "has_logo": false,\n'
        '  "can_flip_horizontal": false,\n'
        '  "flip_risk_reasons": [],\n'
        '  "can_zoom": false,\n'
        '  "max_safe_zoom": 1.0,\n'
        '  "generic_compatibility": false,\n'
        '  "requires_review": false,\n'
        '  "warnings": []\n'
        "}\n\n"
        "Taxonomia obligatoria para primary_theme. Elige exactamente uno:\n"
        + ", ".join(sorted(PRIMARY_THEMES))
        + "\n\n"
        "Reglas:\n"
        "- Todo texto libre en espanol neutro.\n"
        "- primary_theme debe ser exactamente uno de los valores permitidos.\n"
        "- primary_topic debe tener entre 1 y 8 palabras, ser especifico y servir luego como slug de ruta.\n"
        "- tags debe contener entre 5 y 10 etiquetas utiles en espanol, minusculas, sin duplicados y sin terminos genericos como video, archivo, escena o imagen.\n"
        "- visual_presentation representa presentacion visual aparente, no identidad de genero.\n"
        "- visual_presentation debe ser masculine, feminine, mixed, unclear o not_applicable.\n"
        "- person_visibility debe ser clear, partial, back_view, silhouette, occluded o not_applicable.\n"
        "- Si visual_presentation=masculine, search_terms debe incluir persona y hombre.\n"
        "- Si visual_presentation=feminine, search_terms debe incluir persona y mujer.\n"
        "- Si visual_presentation=mixed, search_terms debe incluir personas, hombres, mujeres y grupo.\n"
        "- Si visual_presentation=unclear, search_terms debe incluir persona y opcionalmente silueta.\n"
        "- Para silhouette, back_view u occluded usa persona o silueta salvo evidencia visual muy clara.\n"
        "- fondos-texturas se reserva para fondos visuales, patrones, superficies o material sin una escena o sujeto principal claro.\n"
        "- Si hay una escena concreta con objetos, personas, interior, exterior o lugar reconocible, prefiere personas, objetos, hogar-interiores, ciudad-arquitectura, naturaleza u otro tema especifico.\n"
        '- Si ningun tema encaja: primary_theme="otros", requires_review=true y agrega "taxonomy_low_confidence" en warnings.\n'
        "- No inventes marcas, logos ni texto visible.\n"
        "- max_safe_zoom solo puede ser 1.0, 1.1 o 1.2.\n\n"
        "Contexto del sistema:\n"
        + base_prompt
    )


def parse_reduced_result(text: str) -> NvidiaReducedAssetResult:
    try:
        payload = json.loads(extract_json_object(text))
    except Exception as exc:
        raise NvidiaProviderError("NVIDIA returned invalid JSON") from exc
    payload = normalize_reduced_payload(payload)
    return NvidiaReducedAssetResult.model_validate(payload)


def extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON object not found")
    return stripped[start : end + 1]


def normalize_reduced_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    normalized = dict(payload)
    normalized["primary_topic"] = normalize_primary_topic(normalized.get("primary_topic"))
    warnings = list(normalized.get("warnings") or [])
    if normalized.get("has_visible_text") and normalized.get("can_flip_horizontal"):
        normalized["can_flip_horizontal"] = False
        add_warning(warnings, "flip_disabled_visible_text")
    if normalized.get("has_logo") and normalized.get("can_flip_horizontal"):
        normalized["can_flip_horizontal"] = False
        add_warning(warnings, "flip_disabled_logo")

    zoom = round(float(normalized.get("max_safe_zoom") or 1.0), 1)
    if zoom not in ALLOWED_ZOOMS:
        zoom = min(max(zoom, 1.0), 1.2)
        zoom = max(value for value in ALLOWED_ZOOMS if value <= zoom)
        add_warning(warnings, "zoom_clamped")
    if not normalized.get("can_zoom") and zoom != 1.0:
        zoom = 1.0
        add_warning(warnings, "zoom_reset_when_disabled")
    normalized["max_safe_zoom"] = zoom
    normalized["warnings"] = warnings
    return normalized


def normalize_primary_topic(value: Any) -> str:
    normalized = str(value or "").replace("-", " ").replace("_", " ").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def add_warning(warnings: list[str], value: str) -> None:
    if value not in warnings:
        warnings.append(value)


def reduced_to_asset_result(reduced: NvidiaReducedAssetResult) -> AIAssetEnrichmentResult:
    review_reason = "; ".join(reduced.warnings) if reduced.requires_review or reduced.warnings else None
    return AIAssetEnrichmentResult(
        title=reduced.title_es,
        title_es=reduced.title_es,
        visual_description=reduced.description_es,
        description_es=reduced.description_es,
        action_description=None,
        primary_theme=reduced.primary_theme,
        primary_topic=reduced.primary_topic,
        subject=None,
        action=None,
        context=None,
        suggested_uses=reduced.suggested_uses,
        contains_people=reduced.contains_people,
        people_count=reduced.people_count,
        visual_presentation=reduced.visual_presentation,
        visual_presentation_confidence=reduced.visual_presentation_confidence,
        person_visibility=reduced.person_visibility,
        search_terms=reduced.search_terms,
        can_flip_horizontal=reduced.can_flip_horizontal,
        flip_risk_reasons=reduced.flip_risk_reasons,
        can_zoom=reduced.can_zoom,
        max_safe_zoom=reduced.max_safe_zoom,
        generic_compatibility=reduced.generic_compatibility,
        safe_text_areas=[],
        emotion=None,
        people=None,
        location=None,
        best_for=", ".join(reduced.suggested_uses) if reduced.suggested_uses else None,
        avoid_for=", ".join(reduced.flip_risk_reasons) if reduced.flip_risk_reasons else None,
        negative_keywords=None,
        shot_type="unknown",
        camera_motion="unknown",
        subject_position="unknown",
        visual_energy="unknown",
        pacing="unknown",
        best_scene_role="unknown",
        has_visible_text=reduced.has_visible_text,
        visible_text=None,
        text_language=None,
        has_logo=reduced.has_logo,
        has_watermark=False,
        has_faces=False,
        has_hands=False,
        has_product=False,
        has_directional_motion=False,
        safe_for_subtitles=not reduced.has_visible_text,
        safe_for_text_overlay=True,
        overlay_safe_area="unknown",
        flip_horizontal_allowed=reduced.can_flip_horizontal,
        flip_vertical_allowed=False,
        crop_allowed=True,
        zoom_allowed=reduced.can_zoom,
        speed_change_allowed=True,
        reverse_allowed=False,
        color_grade_allowed=True,
        loopable=False,
        similarity_group=reduced.primary_topic,
        keywords=[
            AIKeyword(keyword=tag, category="concept", weight=1.0, confidence=0.82, language="es")
            for tag in reduced.tags
        ],
        search_text=" ".join([reduced.title_es, reduced.primary_topic, *reduced.tags, *reduced.search_terms]).strip(),
        embedding_text=reduced.description_es,
        confidence=0.82 if not reduced.requires_review else 0.6,
        needs_human_review=reduced.requires_review,
        review_reason=review_reason,
    )
