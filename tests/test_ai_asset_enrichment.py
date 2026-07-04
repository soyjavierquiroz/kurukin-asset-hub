from __future__ import annotations

from base64 import b64encode
from collections.abc import Generator
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db_session
from app.main import create_app
from app.models import Asset, AssetAIAnalysis, AssetKeyword, Source
from app.schemas.ai_enrichment import AIAssetEnrichmentResult, build_openai_strict_json_schema
from app.services import ai_asset_enrichment
from app.services.ai_prompts import build_asset_enrichment_prompt
from app.services.ai_providers import openai_provider
from app.services.ai_asset_enrichment import AssetImageInputs, enrich_asset_with_ai


def auth_header(username: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def make_test_client() -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session() -> Generator[Session, None, None]:
        with session_factory() as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app), session_factory


def seed_asset(
    session: Session,
    uid: str = "asset_ai_001",
    with_preview: bool = True,
    asset_type: str = "image",
) -> Asset:
    source = Source(
        source_id=f"source_{uid}",
        provider="google_drive",
        label="Drive",
        rclone_remote="gdrive_code_x",
        root_path="assets",
    )
    asset = Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_code_x",
        remote_path=f"assets/{uid}.jpg",
        filename=f"{uid}.jpg",
        type=asset_type,
        preview_path=f"previews/{uid}/preview.jpg" if with_preview else None,
        thumbnail_path=f"previews/{uid}/thumbnail.jpg" if with_preview else None,
    )
    session.add(asset)
    session.commit()
    return asset


def valid_ai_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Warm kitchen detail",
        "visual_description": "A close detail shot of a calm kitchen scene.",
        "action_description": "Hands arrange a cup on a counter.",
        "emotion": "calm",
        "people": "hands only",
        "location": "kitchen",
        "best_for": "quiet explanatory b-roll",
        "avoid_for": "high-energy hooks",
        "negative_keywords": "chaotic, loud",
        "shot_type": "detail",
        "camera_motion": "static",
        "subject_position": "center",
        "visual_energy": "low",
        "pacing": "slow",
        "best_scene_role": "broll",
        "has_visible_text": True,
        "visible_text": "Cafe",
        "text_language": "en",
        "has_logo": False,
        "has_watermark": False,
        "has_faces": False,
        "has_hands": True,
        "has_product": True,
        "has_directional_motion": False,
        "safe_for_subtitles": True,
        "safe_for_text_overlay": True,
        "overlay_safe_area": "top",
        "flip_horizontal_allowed": True,
        "flip_vertical_allowed": False,
        "crop_allowed": True,
        "zoom_allowed": True,
        "speed_change_allowed": True,
        "reverse_allowed": False,
        "color_grade_allowed": True,
        "loopable": True,
        "similarity_group": "kitchen-calm",
        "keywords": [
            {
                "keyword": "Kitchen",
                "category": "location",
                "weight": 1.4,
                "confidence": 0.91,
                "language": "en",
            }
        ],
        "search_text": "kitchen calm hands cup",
        "embedding_text": "Calm kitchen b-roll with hands arranging a cup.",
        "confidence": 0.92,
        "needs_human_review": False,
        "review_reason": None,
    }
    payload.update(overrides)
    return payload


def enable_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv("AI_MODEL", "test-vision-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    get_settings.cache_clear()


def contains_key(schema: object, key: str) -> bool:
    if isinstance(schema, dict):
        return key in schema or any(contains_key(value, key) for value in schema.values())
    if isinstance(schema, list):
        return any(contains_key(value, key) for value in schema)
    return False


def collect_object_schemas(schema: object) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                objects.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema)
    return objects


def mock_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ai_asset_enrichment,
        "collect_asset_images",
        lambda asset: AssetImageInputs([Path(__file__)], "thumbnail"),
    )


def test_ai_schema_validates_result() -> None:
    result = AIAssetEnrichmentResult.model_validate(valid_ai_payload())

    assert result.visual_description == "A close detail shot of a calm kitchen scene."
    assert result.keywords[0].category == "location"


def test_ai_schema_rejects_invalid_result() -> None:
    with pytest.raises(ValidationError):
        AIAssetEnrichmentResult.model_validate(valid_ai_payload(shot_type="macro"))


def test_ai_prompt_requests_spanish_output() -> None:
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        asset = seed_asset(session)

        prompt = build_asset_enrichment_prompt(asset)

    assert "Responde en español neutro." in prompt
    assert "Todos los campos de texto libre deben estar en español." in prompt
    assert "Todas las keywords deben estar en español" in prompt


def test_ai_prompt_keeps_enum_values_exact() -> None:
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        asset = seed_asset(session)

        prompt = build_asset_enrichment_prompt(asset)

    assert "Mantén los valores enum exactamente como se definen en el schema" in prompt
    assert "aunque estén en inglés" in prompt


def test_openai_strict_schema_root_requires_all_properties() -> None:
    schema = build_openai_strict_json_schema(AIAssetEnrichmentResult)

    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_openai_strict_schema_keyword_requires_all_properties() -> None:
    schema = build_openai_strict_json_schema(AIAssetEnrichmentResult)
    keyword_schema = schema["$defs"]["AIKeyword"]

    assert set(keyword_schema["required"]) == set(keyword_schema["properties"])
    assert {
        "keyword",
        "category",
        "weight",
        "confidence",
        "language",
    }.issubset(keyword_schema["required"])


def test_openai_strict_schema_removes_defaults() -> None:
    schema = build_openai_strict_json_schema(AIAssetEnrichmentResult)

    assert not contains_key(schema, "default")


def test_openai_strict_schema_requires_every_object_property() -> None:
    schema = build_openai_strict_json_schema(AIAssetEnrichmentResult)

    for object_schema in collect_object_schemas(schema):
        assert set(object_schema["required"]) == set(object_schema["properties"])
        assert object_schema["additionalProperties"] is False


def test_openai_provider_sends_strict_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_ai(monkeypatch)
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs: object) -> object:
            captured["kwargs"] = kwargs
            return SimpleNamespace(output_text=json.dumps(valid_ai_payload()))

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    result = openai_provider.call_openai_vision("prompt", [Path(__file__)])

    request = captured["kwargs"]
    assert isinstance(request, dict)
    text = request["text"]
    assert isinstance(text, dict)
    response_format = text["format"]
    assert isinstance(response_format, dict)
    schema = response_format["schema"]
    assert response_format["name"] == "asset_enrichment"
    assert response_format["strict"] is True
    assert isinstance(schema, dict)
    assert set(schema["required"]) == set(schema["properties"])
    assert not contains_key(schema, "default")
    assert result.keywords[0].keyword == "Kitchen"


def test_enrich_asset_skipped_without_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_ai(monkeypatch)
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        asset = seed_asset(session, with_preview=False)

        enriched = enrich_asset_with_ai(session, asset.id)

        assert enriched.ai_enrichment_status == "skipped"
        assert enriched.review_reason == "Missing preview or thumbnail"


def test_enrich_asset_applies_metadata_flags_keywords_and_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_ai(monkeypatch)
    mock_inputs(monkeypatch)
    monkeypatch.setattr(
        ai_asset_enrichment,
        "call_openai_vision",
        lambda prompt, paths: AIAssetEnrichmentResult.model_validate(valid_ai_payload()),
    )
    client, session_factory = make_test_client()
    del client

    with session_factory() as session:
        asset = seed_asset(session)
        enriched = enrich_asset_with_ai(session, asset.id)

        assert enriched.ai_enrichment_status == "ready"
        assert enriched.visual_description == "A close detail shot of a calm kitchen scene."
        assert enriched.best_for == "quiet explanatory b-roll"
        assert enriched.avoid_for == "high-energy hooks"
        assert enriched.has_visible_text is True
        assert enriched.has_logo is False
        assert enriched.flip_horizontal_allowed is True
        assert enriched.safe_for_subtitles is True
        keyword = session.scalar(select(AssetKeyword).where(AssetKeyword.asset_id == asset.id))
        assert keyword is not None
        assert keyword.keyword == "kitchen"
        assert keyword.source == "ai"
        analysis = session.scalar(select(AssetAIAnalysis).where(AssetAIAnalysis.asset_id == asset.id))
        assert analysis is not None
        assert analysis.result_json["visual_description"] == enriched.visual_description


def test_ai_keywords_default_to_spanish_when_language_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_ai(monkeypatch)
    mock_inputs(monkeypatch)
    monkeypatch.setenv("AI_OUTPUT_LANGUAGE", "es")
    get_settings.cache_clear()
    monkeypatch.setattr(
        ai_asset_enrichment,
        "call_openai_vision",
        lambda prompt, paths: AIAssetEnrichmentResult.model_validate(
            valid_ai_payload(
                keywords=[
                    {
                        "keyword": "Mujer",
                        "category": "subject",
                        "weight": 1.0,
                        "confidence": 0.9,
                        "language": None,
                    },
                    {
                        "keyword": "Sonrisa",
                        "category": "mood",
                        "weight": 0.8,
                        "confidence": 0.88,
                        "language": "",
                    },
                ]
            )
        ),
    )
    client, session_factory = make_test_client()
    del client

    with session_factory() as session:
        asset = seed_asset(session)
        session.add(
            AssetKeyword(
                asset=asset,
                keyword="manual keeper",
                category="concept",
                weight=1.0,
                confidence=1.0,
                source="manual",
                language="und",
            )
        )
        session.commit()

        enrich_asset_with_ai(session, asset.id, force=True)
        keywords = session.scalars(select(AssetKeyword).where(AssetKeyword.asset_id == asset.id)).all()

        ai_languages = {
            keyword.keyword: keyword.language for keyword in keywords if keyword.source == "ai"
        }
        manual_keyword = next(keyword for keyword in keywords if keyword.source == "manual")
        assert ai_languages == {"mujer": "es", "sonrisa": "es"}
        assert manual_keyword.language == "und"


def test_force_replaces_ai_keywords_and_preserves_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_ai(monkeypatch)
    mock_inputs(monkeypatch)
    monkeypatch.setattr(
        ai_asset_enrichment,
        "call_openai_vision",
        lambda prompt, paths: AIAssetEnrichmentResult.model_validate(
            valid_ai_payload(
                keywords=[
                    {
                        "keyword": "New keyword",
                        "category": "concept",
                        "weight": 2.0,
                        "confidence": 0.9,
                        "language": "en",
                    }
                ]
            )
        ),
    )
    client, session_factory = make_test_client()
    del client

    with session_factory() as session:
        asset = seed_asset(session)
        session.add_all(
            [
                AssetKeyword(
                    asset=asset,
                    keyword="old ai",
                    category="concept",
                    weight=1.0,
                    confidence=0.8,
                    source="ai",
                    language="en",
                ),
                AssetKeyword(
                    asset=asset,
                    keyword="manual keeper",
                    category="concept",
                    weight=1.0,
                    confidence=1.0,
                    source="manual",
                    language="en",
                ),
            ]
        )
        session.commit()

        enrich_asset_with_ai(session, asset.id, force=True)
        keywords = {
            (keyword.keyword, keyword.source)
            for keyword in session.scalars(
                select(AssetKeyword).where(AssetKeyword.asset_id == asset.id)
            )
        }

        assert ("old ai", "ai") not in keywords
        assert ("new keyword", "ai") in keywords
        assert ("manual keeper", "manual") in keywords


def test_failure_marks_asset_failed_and_sanitizes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_ai(monkeypatch)
    mock_inputs(monkeypatch)
    monkeypatch.setattr(
        ai_asset_enrichment,
        "call_openai_vision",
        lambda prompt, paths: (_ for _ in ()).throw(RuntimeError("provider failed sk-test-secret")),
    )
    client, session_factory = make_test_client()
    del client

    with session_factory() as session:
        asset = seed_asset(session)
        enriched = enrich_asset_with_ai(session, asset.id)

        assert enriched.ai_enrichment_status == "failed"
        assert "sk-test-secret" not in (enriched.ai_error or "")


def test_cli_dry_run_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    enable_ai(monkeypatch)
    monkeypatch.setattr(
        ai_asset_enrichment,
        "call_openai_vision",
        lambda prompt, paths: pytest.fail("provider should not be called during dry-run"),
    )
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        asset = seed_asset(session)

    import scripts.enrich_assets_ai as cli

    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    monkeypatch.setattr(sys, "argv", ["enrich_assets_ai.py", "--asset-id", str(asset.id), "--dry-run"])

    assert cli.main() == 0
    assert "processed=1" in capsys.readouterr().out


def test_ui_detail_shows_ai_section() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        asset = seed_asset(session)

    response = client.get(f"/assets/{asset.id}", headers=auth_header())

    assert response.status_code == 200
    assert "AI Enrichment" in response.text
    assert "Run AI Enrichment" in response.text


def test_ui_list_shows_review_badge_and_ai_status_near_filename() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        review_asset = seed_asset(session, uid="review_asset")
        review_asset.ai_enrichment_status = "needs_review"
        review_asset.ai_enrichment_confidence = 0.63
        review_asset.needs_human_review = True
        review_asset.review_reason = "Texto visible ambiguo"
        session.commit()

    response = client.get("/assets", headers=auth_header())

    assert response.status_code == 200
    html = response.text
    filename_index = html.index("review_asset.jpg")
    asset_row_start = filename_index
    asset_row_end = filename_index + 1200
    asset_row = html[asset_row_start:asset_row_end]
    assert "Revisar" in asset_row
    assert "AI needs_review" in asset_row
    assert "AI 0.63" in asset_row
    assert "Texto visible ambiguo" in html


def test_ui_detail_shows_review_reason_panel() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        asset = seed_asset(session, uid="detail_review_asset")
        asset.ai_enrichment_status = "needs_review"
        asset.ai_enrichment_confidence = 0.61
        asset.needs_human_review = True
        asset.review_reason = "Logo visible requiere validacion"
        session.commit()

    response = client.get(f"/assets/{asset.id}", headers=auth_header())

    assert response.status_code == 200
    assert "Needs review" in response.text
    assert "Logo visible requiere validacion" in response.text
    assert "AI 0.61" in response.text


def test_ui_needs_review_filters_true_false() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        review_asset = seed_asset(session, uid="needs_review_true")
        review_asset.needs_human_review = True
        review_asset.ai_enrichment_status = "needs_review"
        ready_asset = seed_asset(session, uid="needs_review_false")
        ready_asset.needs_human_review = False
        ready_asset.ai_enrichment_status = "ready"
        session.commit()

    true_response = client.get("/assets?needs_review=true", headers=auth_header())
    false_response = client.get("/assets?needs_review=false", headers=auth_header())
    all_response = client.get("/assets", headers=auth_header())

    assert true_response.status_code == 200
    assert "needs_review_true.jpg" in true_response.text
    assert "needs_review_false.jpg" not in true_response.text
    assert false_response.status_code == 200
    assert "needs_review_false.jpg" in false_response.text
    assert "needs_review_true.jpg" not in false_response.text
    assert all_response.status_code == 200
    assert "needs_review_true.jpg" in all_response.text
    assert "needs_review_false.jpg" in all_response.text


def test_ai_routes_require_auth_and_api_key() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        asset = seed_asset(session)

    web_response = client.post(f"/assets/{asset.id}/ai-enrich")
    api_response = client.post(f"/api/assets/{asset.id}/ai-enrich")

    assert web_response.status_code == 401
    assert api_response.status_code == 401


def test_api_ai_enrich_accepts_api_key_dry_run() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        asset = seed_asset(session)

    response = client.post(
        f"/api/assets/{asset.id}/ai-enrich",
        params={"dry_run": "true"},
        headers={"X-Asset-Hub-Api-Key": "test-api-key"},
    )

    assert response.status_code == 200
    assert response.json()["ai_enrichment_status"] == "pending"
