from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from app.models import Asset
from app.services.people_metadata import HUMAN_PRESENTATIONS, normalize_search_terms


def normalize_search_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    ascii_text = re.sub(r"[^\w\s.-]+", " ", ascii_text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", ascii_text.lower()).strip()


def tokenize_query(q: str | None) -> list[str]:
    normalized = normalize_search_text(q)
    tokens: list[str] = []
    for token in normalized.split():
        if len(token) < 3 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens


def build_asset_search_blob(asset: Asset) -> str:
    values: list[str | None] = [
        asset.filename,
        asset.title,
        asset.search_text,
        asset.embedding_text,
        asset.visual_description,
        asset.action_description,
        asset.best_for,
        asset.avoid_for,
        asset.negative_keywords,
        asset.brand.slug if asset.brand else None,
        asset.brand.name if asset.brand else None,
        asset.product.slug if asset.product else None,
        asset.product.name if asset.product else None,
    ]
    for niche in asset.niches:
        values.extend([niche.slug, niche.name])
    for keyword in asset.keywords:
        values.append(keyword.keyword)
    values.extend(asset_people_search_terms(asset))
    return normalize_search_text(" ".join(value for value in values if value))


def score_asset_for_query(asset: Asset, tokens: list[str]) -> float:
    if not tokens:
        return score_asset_without_query(asset)

    score = 0.0
    normalized_fields = {
        "filename": normalize_search_text(asset.filename),
        "title": normalize_search_text(asset.title),
        "search_text": normalize_search_text(asset.search_text),
        "embedding_text": normalize_search_text(asset.embedding_text),
        "visual_description": normalize_search_text(asset.visual_description),
        "action_description": normalize_search_text(asset.action_description),
        "best_for": normalize_search_text(asset.best_for),
        "avoid_for": normalize_search_text(asset.avoid_for),
        "negative_keywords": normalize_search_text(asset.negative_keywords),
        "brand": normalize_search_text(
            " ".join(value for value in [asset.brand.slug, asset.brand.name] if value)
            if asset.brand
            else None
        ),
        "product": normalize_search_text(
            " ".join(value for value in [asset.product.slug, asset.product.name] if value)
            if asset.product
            else None
        ),
        "niches": normalize_search_text(
            " ".join(
                value
                for niche in asset.niches
                for value in (niche.slug, niche.name)
                if value
            )
        ),
        "people_search_terms": normalize_search_text(" ".join(asset_people_search_terms(asset))),
    }
    normalized_keywords = [normalize_search_text(keyword.keyword) for keyword in asset.keywords]
    blob = build_asset_search_blob(asset)

    for token in tokens:
        if any(token == keyword for keyword in normalized_keywords):
            score += 8.0
        elif any(token in keyword for keyword in normalized_keywords):
            score += 5.0

        if token in normalized_fields["filename"]:
            score += 6.0
        if token in normalized_fields["title"]:
            score += 6.0
        if token in normalized_fields["search_text"]:
            score += 4.0
        if token in normalized_fields["embedding_text"]:
            score += 4.0
        if token in normalized_fields["visual_description"]:
            score += 3.0
        if token in normalized_fields["action_description"]:
            score += 3.0
        if token in normalized_fields["best_for"]:
            score += 3.0
        if token in normalized_fields["avoid_for"]:
            score += 2.0
        if token in normalized_fields["negative_keywords"]:
            score += 1.0
        if token in normalized_fields["brand"]:
            score += 3.0
        if token in normalized_fields["product"]:
            score += 3.0
        if token in normalized_fields["niches"]:
            score += 3.0
        if token in normalized_fields["people_search_terms"]:
            score += 8.0
        if token in blob:
            score += 0.5

    matched_tokens = sum(1 for token in tokens if token in blob)
    if matched_tokens == len(tokens):
        score += 2.0
    return score


def score_asset_without_query(asset: Asset) -> float:
    score = asset.quality_score or 0.0
    if asset.ai_enrichment_confidence is not None:
        score += asset.ai_enrichment_confidence
    if asset.preview_status == "ready":
        score += 0.25
    return score


def asset_people_search_terms(asset: Asset) -> list[str]:
    analysis = latest_people_analysis(asset)
    if not analysis:
        return []
    return normalize_search_terms([str(term) for term in analysis.get("search_terms") or []])


def latest_people_analysis(asset: Asset) -> dict[str, object] | None:
    analyses = sorted(asset.ai_analyses, key=lambda analysis: analysis.created_at or datetime.min, reverse=True)
    for analysis in analyses:
        result = analysis.result_json or {}
        if isinstance(result, dict) and result.get("visual_presentation"):
            return result
    return None


def asset_visual_presentation(asset: Asset) -> str | None:
    analysis = latest_people_analysis(asset)
    if not analysis:
        return None
    presentation = analysis.get("visual_presentation")
    return str(presentation) if presentation is not None else None


def asset_matches_people_query(asset: Asset, tokens: list[str]) -> bool:
    queried = set(tokens)
    if not queried & {"persona", "personas", "hombre", "hombres", "mujer", "mujeres"}:
        return True
    presentation = asset_visual_presentation(asset)
    if "hombre" in queried or "hombres" in queried:
        return presentation == "masculine"
    if "mujer" in queried or "mujeres" in queried:
        return presentation == "feminine"
    if "persona" in queried or "personas" in queried:
        return presentation in HUMAN_PRESENTATIONS
    return True
