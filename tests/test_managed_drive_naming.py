from __future__ import annotations

from app.services import managed_drive_pilot
from app.services.managed_drive_pilot import (
    aspect_ratio,
    clean_semantic_slug,
    compact_orientation,
    humanize_slug,
    near,
    normalize_existing_enum,
    pilot_orientation,
    safe_original_filename,
    sanitized_list,
    short_id,
    shot_type_es,
    slugify,
    split_text_list,
    text_or_none,
    trim_semantic_stem,
)


def test_naming_helpers_remain_importable_from_managed_drive_pilot() -> None:
    assert near is managed_drive_pilot.near
    assert aspect_ratio is managed_drive_pilot.aspect_ratio
    assert pilot_orientation is managed_drive_pilot.pilot_orientation
    assert slugify is managed_drive_pilot.slugify
    assert sanitized_list is managed_drive_pilot.sanitized_list
    assert split_text_list is managed_drive_pilot.split_text_list
    assert short_id is managed_drive_pilot.short_id
    assert safe_original_filename is managed_drive_pilot.safe_original_filename
    assert text_or_none is managed_drive_pilot.text_or_none
    assert normalize_existing_enum is managed_drive_pilot.normalize_existing_enum
    assert humanize_slug is managed_drive_pilot.humanize_slug
    assert clean_semantic_slug is managed_drive_pilot.clean_semantic_slug
    assert trim_semantic_stem is managed_drive_pilot.trim_semantic_stem
    assert compact_orientation is managed_drive_pilot.compact_orientation
    assert shot_type_es is managed_drive_pilot.shot_type_es


def test_slugify_keeps_existing_normalization() -> None:
    assert slugify("Grandiosa Mujer / Acción Ñ") == "grandiosa-mujer-accion-n"
    assert slugify("  Transferencia_de_Energía_Mágica  ") == "transferencia-de-energia-magica"


def test_pilot_orientation_keeps_existing_cases() -> None:
    assert pilot_orientation(1080, 1920) == "vertical-9x16"
    assert pilot_orientation(1920, 1080) == "horizontal-16x9"
    assert pilot_orientation(1000, 1000) == "cuadrado-1x1"
    assert pilot_orientation(None, 1080) == "otro"


def test_compact_orientation_keeps_existing_mapping() -> None:
    assert compact_orientation("horizontal-16x9") == "16x9"
    assert compact_orientation("vertical-9x16") == "9x16"
    assert compact_orientation("vertical-4x5") == "4x5"
    assert compact_orientation("cuadrado-1x1") == "1x1"
    assert compact_orientation("panoramico") == "panoramico"


def test_shot_type_es_keeps_existing_mapping() -> None:
    assert shot_type_es("closeup") == "primer-plano"
    assert shot_type_es("medium") == "plano-medio"
    assert shot_type_es("wide") == "plano-abierto"
    assert shot_type_es("detail") == "detalle"
    assert shot_type_es("establishing") == "plano-general"
    assert shot_type_es("unknown") == "unknown"
