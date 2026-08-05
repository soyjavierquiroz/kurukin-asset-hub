from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.managed_drive.naming import slugify

if TYPE_CHECKING:
    from app.models import Asset, Brand
    from app.services.managed_drive.contracts import Classification, IngestRequest


def compact_folder_primary_topic(asset: Asset) -> str:
    return "compact_review" if asset.status == "review_required" or asset.needs_human_review else "compact"


def folder_context_slug(
    request_data: IngestRequest,
    asset: Asset | None = None,
    brand: Brand | None = None,
) -> str | None:
    if request_data.scope == "generic":
        return None
    if request_data.scope == "brand":
        if brand is not None:
            return brand.slug
        if asset is not None and asset.brand is not None:
            return asset.brand.slug
        return slugify(request_data.brand_slug or "")
    if asset is not None and asset.title_slug:
        return asset.title_slug
    return slugify(request_data.title or "")


def legacy_context_slug(asset: Asset) -> str | None:
    if asset.scope == "generic":
        return None
    if asset.scope == "brand":
        return asset.brand.slug if asset.brand else None
    return asset.title_slug or slugify(asset.title_name or "")


def route_parts(
    request_data: IngestRequest,
    brand: Brand | None,
    media_type: str,
    classification: Classification,
    orientation: str,
    batch_number: int = 1,
    requires_review: bool = False,
    asset: Asset | None = None,
) -> list[str]:
    batch = f"lote-{batch_number:04d}"
    if requires_review or classification.ambiguous:
        parts = ["90_revision", request_data.scope]
        context = folder_context_slug(request_data, asset, brand)
        if context:
            parts.append(context)
        return [*parts, media_type, batch]
    if request_data.scope == "generic":
        return ["10_genericos", media_type, batch]
    if request_data.scope == "brand":
        return [
            "20_marcas",
            folder_context_slug(request_data, asset, brand) or slugify(request_data.brand_slug or ""),
            request_data.collection,
            media_type,
            batch,
        ]
    title_slug = slugify(request_data.title or "")
    if asset is not None:
        title_slug = asset.title_slug or title_slug
    if request_data.title_type == "movie":
        return ["30_peliculas_series", "peliculas", title_slug, "brolls-generales", media_type, batch]
    parts = ["30_peliculas_series", "series", title_slug]
    if request_data.season is not None and request_data.episode is not None:
        parts.extend([f"temporada-{request_data.season:02d}", f"episodio-{request_data.episode:02d}"])
    else:
        parts.append("brolls-generales")
    parts.extend([media_type, batch])
    return parts
