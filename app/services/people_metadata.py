from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

VisualPresentation = Literal["masculine", "feminine", "mixed", "unclear", "not_applicable"]
PersonVisibility = Literal["clear", "partial", "back_view", "silhouette", "occluded", "not_applicable"]
SourcePresentationHint = Literal["masculine", "feminine", "unclear"]

VISUAL_PRESENTATION_VALUES = ("masculine", "feminine", "mixed", "unclear", "not_applicable")
PERSON_VISIBILITY_VALUES = ("clear", "partial", "back_view", "silhouette", "occluded", "not_applicable")
SOURCE_PRESENTATION_HINT_VALUES = ("masculine", "feminine", "unclear")
HUMAN_PRESENTATIONS = {"masculine", "feminine", "mixed", "unclear"}
GENDERED_PRESENTATIONS = {"masculine", "feminine"}
GENDERED_TERMS = {"hombre", "hombres", "mujer", "mujeres"}
SOURCE_HINT_PATTERNS = (
    ("masculine", re.compile(r"(^|[^a-z0-9])(a_man|man|male)([^a-z0-9]|$)")),
    ("feminine", re.compile(r"(^|[^a-z0-9])(a_woman|woman|female)([^a-z0-9]|$)")),
    ("unclear", re.compile(r"(^|[^a-z0-9])person([^a-z0-9]|$)")),
)


def normalize_for_people(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text.lower()).strip()


def source_presentation_hint(filename: str | None) -> SourcePresentationHint | None:
    normalized = normalize_for_people(filename)
    underscored = re.sub(r"\s+", "_", normalized)
    candidates = f"{normalized} {underscored}"
    for hint, pattern in SOURCE_HINT_PATTERNS:
        if pattern.search(candidates):
            return hint  # type: ignore[return-value]
    return None


def normalize_search_terms(values: list[str] | None) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        term = normalize_term(value)
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def normalize_term(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def people_search_terms(
    visual_presentation: str,
    person_visibility: str = "not_applicable",
    base_terms: list[str] | None = None,
) -> list[str]:
    terms = normalize_search_terms(base_terms)
    if visual_presentation == "masculine":
        terms = append_missing(terms, ["persona", "hombre"])
    elif visual_presentation == "feminine":
        terms = append_missing(terms, ["persona", "mujer"])
    elif visual_presentation == "mixed":
        terms = append_missing(terms, ["personas", "hombres", "mujeres", "grupo"])
    elif visual_presentation == "unclear":
        terms = [term for term in terms if term not in GENDERED_TERMS]
        terms = append_missing(terms, ["persona"])
        if person_visibility == "silhouette":
            terms = append_missing(terms, ["silueta"])
    return terms


def append_missing(values: list[str], additions: list[str]) -> list[str]:
    result = list(values)
    seen = set(result)
    for value in additions:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def gendered_metadata_allowed(confidence: float | None, visibility: str | None) -> bool:
    return float(confidence or 0.0) >= 0.8 and visibility in {"clear", "partial"}


def neutralize_gendered_terms(value: str | None) -> str | None:
    if value is None:
        return None
    replacements = {
        r"\bhombres\b": "personas",
        r"\bhombre\b": "persona",
        r"\bmujeres\b": "personas",
        r"\bmujer\b": "persona",
    }
    updated = value
    for pattern, replacement in replacements.items():
        updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", updated).strip()


def neutralize_gendered_tags(values: list[str]) -> list[str]:
    return normalize_search_terms(
        [neutralize_gendered_terms(value) or "" for value in values if value]
    )
