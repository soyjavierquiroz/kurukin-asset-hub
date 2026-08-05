from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import managed_drive_pilot
from app.services.managed_drive import contracts


MOVED_CONTRACTS = (
    "DriveFile",
    "PreflightRequest",
    "IngestRequest",
    "TechnicalResult",
    "PreviewResult",
    "ManagedAIResult",
    "Classification",
    "BatchScope",
    "BatchRequest",
    "ReviewApproval",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = PROJECT_ROOT / "app/services/managed_drive_pilot.py"
LAYOUT_PATH = PROJECT_ROOT / "app/services/managed_drive/layout.py"
CONTRACTS_PATH = PROJECT_ROOT / "app/services/managed_drive/contracts.py"


def class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_contracts_are_reexported_from_pilot_with_identity() -> None:
    for name in MOVED_CONTRACTS:
        assert getattr(managed_drive_pilot, name) is getattr(contracts, name)


def test_pilot_has_no_duplicate_class_definitions_for_moved_contracts() -> None:
    assert class_names(PILOT_PATH).isdisjoint(MOVED_CONTRACTS)


def test_ingest_request_defaults_are_unchanged() -> None:
    request = contracts.IngestRequest("source", "root", "generic")

    assert request.limit == 10
    assert request.apply is False
    assert request.dry_run is True
    assert request.file_id is None
    assert request.brand_slug is None
    assert request.collection == "evergreen"
    assert request.title_type is None
    assert request.title is None
    assert request.season is None
    assert request.episode is None
    assert request.replan is False
    assert request.simulate_drive_mutation is False


def test_batch_request_defaults_are_unchanged() -> None:
    request = contracts.BatchRequest()

    assert request.apply is False
    assert request.max_total == 10
    assert request.max_per_scope == 5
    assert request.concurrency == 1
    assert request.nvidia_pause_seconds == 0.75
    assert request.only_file_ids == ()


def test_managed_ai_result_construction_and_serialization_are_unchanged() -> None:
    result = contracts.ManagedAIResult(
        title_es="Plano general",
        tags=["ciudad", "noche"],
        confidence={"overall": 0.8},
        visual_presentation="unclear",
        person_visibility="silhouette",
        source_presentation_hint="unclear",
        frames_analyzed=3,
        ignored_runtime_field="ignored",
    )

    assert result.model_dump() == {
        "title_es": "Plano general",
        "description_es": None,
        "primary_theme": None,
        "primary_topic": None,
        "subject": None,
        "action": None,
        "context": None,
        "tags": ["ciudad", "noche"],
        "tags_source": None,
        "suggested_uses": [],
        "contains_people": False,
        "people_count": None,
        "visual_presentation": "unclear",
        "visual_presentation_confidence": 0.0,
        "person_visibility": "silhouette",
        "search_terms": [],
        "source_presentation_hint": "unclear",
        "can_flip_horizontal": True,
        "flip_risk_reasons": [],
        "can_zoom": True,
        "max_safe_zoom": 1.0,
        "has_visible_text": False,
        "has_logo": False,
        "generic_compatibility": False,
        "camera_motion": "unknown",
        "shot_type": "unknown",
        "subject_position": "unknown",
        "safe_text_areas": [],
        "confidence": {"overall": 0.8},
        "requires_review": False,
        "warnings": [],
        "ai_analysis_mode": "FALLBACK",
        "ai_provider": None,
        "ai_model": None,
        "frames_analyzed": 3,
    }


def test_classification_behavior_is_unchanged() -> None:
    classification = contracts.Classification("personas", "bienestar yoga")

    assert classification.primary_theme == "personas"
    assert classification.primary_topic == "bienestar yoga"
    assert classification.ambiguous is False
    assert classification == contracts.Classification("personas", "bienestar yoga")
    with pytest.raises(FrozenInstanceError):
        classification.ambiguous = True  # type: ignore[misc]


def test_managed_ai_result_literal_validation_is_unchanged() -> None:
    with pytest.raises(ValidationError):
        contracts.ManagedAIResult(visual_presentation="invalid")


def test_layout_no_longer_imports_managed_drive_pilot() -> None:
    assert "managed_drive_pilot" not in LAYOUT_PATH.read_text()


def test_contracts_have_no_forbidden_runtime_imports() -> None:
    forbidden_roots = {
        "sqlalchemy",
        "app.models",
        "app.config",
        "app.services.asset_preview",
        "app.services.ai_asset_enrichment",
        "app.services.ai_providers",
    }

    modules = imported_modules(CONTRACTS_PATH)

    assert all(not module.startswith(tuple(forbidden_roots)) for module in modules)
    assert not any(module.startswith("app.") for module in modules)
