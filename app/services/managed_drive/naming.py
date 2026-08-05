from __future__ import annotations

import hashlib
from pathlib import Path
import re

SAFE_STEM_RE = re.compile(r"[^a-z0-9]+")


def clean_semantic_slug(value: str | None) -> str:
    slug = slugify(value or "")
    blocked = {"mp4", "jpg", "jpeg", "png", "mov", "webm", "segment", "video", "imagen", "archivo"}
    parts = [part for part in slug.split("-") if part and part not in blocked and not part.isdigit()]
    return "-".join(parts)


def trim_semantic_stem(value: str, limit: int) -> str:
    clean = clean_semantic_slug(value)
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit("-", 1)[0].strip("-") or clean[:limit].strip("-")


def compact_orientation(value: str) -> str:
    return {
        "horizontal-16x9": "16x9",
        "vertical-9x16": "9x16",
        "vertical-4x5": "4x5",
        "cuadrado-1x1": "1x1",
    }.get(value, value)


def shot_type_es(value: str) -> str:
    return {
        "closeup": "primer-plano",
        "medium": "plano-medio",
        "wide": "plano-abierto",
        "detail": "detalle",
        "establishing": "plano-general",
    }.get(value, value)


def pilot_orientation(width: int | None, height: int | None) -> str:
    if not width or not height or width <= 0 or height <= 0:
        return "otro"
    ratio = width / height
    if near(ratio, 16 / 9, 0.08):
        return "horizontal-16x9"
    if near(ratio, 9 / 16, 0.08):
        return "vertical-9x16"
    if near(ratio, 4 / 5, 0.08):
        return "vertical-4x5"
    if near(ratio, 1, 0.08):
        return "cuadrado-1x1"
    if ratio >= 2.0:
        return "panoramico"
    return "otro"


def aspect_ratio(width: int | None, height: int | None) -> float | None:
    if not width or not height or width <= 0 or height <= 0:
        return None
    return round(width / height, 4)


def near(value: float, target: float, tolerance: float) -> bool:
    return abs(value - target) <= tolerance


def slugify(value: str) -> str:
    normalized = value.strip().lower()
    normalized = (
        normalized.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ñ", "n")
    )
    cleaned = SAFE_STEM_RE.sub("-", normalized).strip("-")
    return cleaned[:120].strip("-")


def sanitized_list(values: list[str], limit: int) -> list[str]:
    cleaned = []
    for value in values[:limit]:
        item = slugify(str(value))
        if item:
            cleaned.append(item)
    return cleaned


def split_text_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,;\n]+", value) if item.strip()][:8]


def short_id(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()
    return digest[:8]


def safe_original_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_. -]+", "_", Path(name).name).strip(" ._")[:180] or "asset"


def text_or_none(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip()[:2000] or None


def normalize_existing_enum(value: str) -> str:
    return value if value else "unknown"


def humanize_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part) or slug
