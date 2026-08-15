import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import String, and_, cast, exists, false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db_session
from app.models import Asset, AssetAIAnalysis, AssetAllowedBrand, AssetKeyword, AssetTag, Brand, Niche, Product
from app.models.asset import ORIENTATION_VALUES, USAGE_SCOPE_VALUES
from app.schemas.asset_selection import AssetSelectionRequest, AssetSelectionResponse
from app.services.ai_asset_enrichment import enrich_asset_with_ai
from app.services.asset_preview import preview_public_url
from app.services.asset_policy import is_asset_eligible_for_search, resolve_asset_search_policy
from app.services.asset_search import asset_matches_people_query, score_asset_for_query, tokenize_query
from app.services.asset_selection import select_assets

router = APIRouter(prefix="/api/assets", tags=["assets"])


class AssetSourcePolicySource(BaseModel):
    scope: str
    brand: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "AssetSourcePolicySource":
        if self.scope not in {"generic", "brand", "title"}:
            raise ValueError(f"Invalid scope: {self.scope}")
        if self.scope == "brand" and not clean_json_value(self.brand):
            raise ValueError("brand source requires brand")
        if self.scope == "title" and not clean_json_value(self.title):
            raise ValueError("title source requires title")
        self.brand = clean_json_value(self.brand)
        self.title = clean_json_value(self.title)
        return self


class AssetSourcePolicy(BaseModel):
    sources: list[AssetSourcePolicySource] = Field(min_length=1)


class AssetJsonSearchRequest(BaseModel):
    query: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    source_policy: AssetSourcePolicy | None = None


def clean_json_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def require_asset_hub_api_key(
    api_key: Annotated[str | None, Header(alias="X-Asset-Hub-Api-Key")] = None,
) -> None:
    expected_api_key = get_settings().asset_hub_api_key
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )
    if not secrets.compare_digest(api_key, expected_api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )


@router.get("/search")
def search_assets(
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
    q: str | None = None,
    brand_slug: str | None = None,
    product_slug: str | None = None,
    niche_slug: str | None = None,
    type: str | None = None,
    orientation: str | None = None,
    scope: str | None = None,
    brand: str | None = None,
    title: str | None = None,
    primary_theme: str | None = None,
    primary_topic: str | None = None,
    asset_status: Annotated[str | None, Query(alias="status")] = None,
    usage_scope: str | None = None,
    include_global_assets: bool = True,
    include_stock_assets: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> dict[str, Any]:
    if orientation and orientation not in ORIENTATION_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid orientation: {orientation}",
        )
    if usage_scope and usage_scope not in USAGE_SCOPE_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid usage_scope: {usage_scope}",
        )
    if scope and scope not in {"generic", "brand", "title"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scope: {scope}",
        )

    if product_slug and not brand_slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="product_slug requires brand_slug",
        )

    filters = [eligible_base_filter()]
    brand_context: Brand | None = None
    product: Product | None = None

    if brand_slug:
        brand_context = session.scalar(
            select(Brand)
            .where(Brand.slug == brand_slug)
            .options(selectinload(Brand.asset_policy))
        )
        if brand_context is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand not found: {brand_slug}",
            )
        filters.append(
            or_(
                Asset.brand_id == brand_context.id,
                Asset.usage_scope == "global",
                allowed_brand_exists(brand_context),
            )
        )
    if brand and not brand_slug:
        brand_record = session.scalar(select(Brand).where(Brand.slug == brand))
        if brand_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand not found: {brand}",
            )
        brand_context = brand_record
        filters.append(Asset.brand_id == brand_record.id)
    if product_slug and brand_context is not None:
        product = session.scalar(
            select(Product)
            .where(Product.brand_id == brand_context.id, Product.slug == product_slug)
            .options(selectinload(Product.asset_policy))
        )
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product not found: {product_slug}",
            )

    if brand_context is None and not brand:
        filters.append(Asset.usage_scope == "global" if include_global_assets else false())

    if not include_stock_assets:
        filters.append(Asset.rights_status != "stock")

    if niche_slug:
        filters.append(Asset.niches.any(Niche.slug == niche_slug))
    if type:
        filters.append(Asset.type == type)
    if orientation:
        filters.append(Asset.orientation == orientation)
    if usage_scope:
        filters.append(Asset.usage_scope == usage_scope)
    if scope:
        filters.append(Asset.scope == scope)
    if title:
        filters.append(Asset.title_slug == title)
    if primary_theme:
        filters.append(Asset.primary_theme == primary_theme)
    if primary_topic:
        filters.append(Asset.primary_topic == primary_topic)
    if asset_status:
        filters.append(Asset.status == asset_status)

    tokens = tokenize_query(q)
    candidate_limit = max(limit * 20, 200)

    query = (
        select(Asset)
        .where(and_(*filters))
        .options(
            selectinload(Asset.source),
            selectinload(Asset.brand),
            selectinload(Asset.product),
            selectinload(Asset.niches),
            selectinload(Asset.tags),
            selectinload(Asset.keywords),
            selectinload(Asset.ai_analyses),
            selectinload(Asset.allowed_brands).selectinload(AssetAllowedBrand.brand),
        )
        .order_by(
            func.coalesce(Asset.quality_score, 0).desc(),
            func.coalesce(Asset.ai_enrichment_confidence, 0).desc(),
            Asset.usage_count.asc(),
            Asset.id.desc(),
        )
        .limit(candidate_limit)
    )
    policy = resolve_asset_search_policy(brand=brand_context, product=product)
    scored_assets: list[tuple[Asset, float]] = []
    for asset in session.scalars(query).all():
        if not is_asset_eligible_for_search(
            asset=asset,
            brand=brand_context,
            product=product,
            include_global_assets=include_global_assets,
            include_stock_assets=include_stock_assets,
            policy=policy,
        ):
            continue
        if not asset_matches_people_query(asset, tokens):
            continue
        score = score_asset_for_query(asset, tokens)
        if tokens and score <= 0:
            continue
        scored_assets.append((asset, score))

    scored_assets.sort(
        key=lambda item: (
            item[1],
            item[0].quality_score or 0,
            item[0].ai_enrichment_confidence or 0,
            item[0].id,
        ),
        reverse=True,
    )
    scored_assets = scored_assets[:limit]

    return {
        "count": len(scored_assets),
        "limit": limit,
        "assets": [serialize_asset(asset, score=score) for asset, score in scored_assets],
    }


@router.post("/search")
def search_assets_json(
    request: AssetJsonSearchRequest,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, Any]:
    source_policy = request.source_policy or AssetSourcePolicy(
        sources=[AssetSourcePolicySource(scope="generic")]
    )
    tokens = tokenize_query(request.query)
    filters = [
        usable_production_asset_filter(),
        source_policy_filter(source_policy),
    ]
    if get_settings().asset_editorial_gate_enabled:
        filters.append(Asset.editorial_status == "searchable")
    text_filter = json_text_search_filter(request.query)
    if text_filter is not None:
        filters.append(text_filter)

    candidate_limit = max(request.limit * 20, 200)
    query = (
        select(Asset)
        .where(and_(*filters))
        .options(
            selectinload(Asset.source),
            selectinload(Asset.brand),
            selectinload(Asset.product),
            selectinload(Asset.niches),
            selectinload(Asset.tags),
            selectinload(Asset.keywords),
            selectinload(Asset.ai_analyses),
        )
        .order_by(
            func.coalesce(Asset.quality_score, 0).desc(),
            func.coalesce(Asset.ai_enrichment_confidence, 0).desc(),
            Asset.usage_count.asc(),
            Asset.id.desc(),
        )
        .limit(candidate_limit)
    )

    scored_assets: list[tuple[Asset, float]] = []
    for asset in session.scalars(query).all():
        score = score_asset_for_query(asset, tokens)
        if tokens and score <= 0:
            continue
        scored_assets.append((asset, score))

    scored_assets.sort(
        key=lambda item: (
            item[1],
            item[0].quality_score or 0,
            item[0].ai_enrichment_confidence or 0,
            item[0].id,
        ),
        reverse=True,
    )
    scored_assets = scored_assets[: request.limit]

    return {
        "query": request.query,
        "source_policy": source_policy.model_dump(),
        "count": len(scored_assets),
        "assets": [serialize_money_printer_asset(asset) for asset, _score in scored_assets],
    }


@router.post("/select", response_model=AssetSelectionResponse)
def api_select_assets(
    request: AssetSelectionRequest,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> AssetSelectionResponse:
    if request.include_restricted:
        raise HTTPException(
            status_code=422,
            detail="include_restricted is not supported for asset selection",
        )
    return select_assets(session, request)


@router.post("/{asset_id}/ai-enrich")
def api_enrich_asset_with_ai(
    asset_id: int,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    asset = enrich_asset_with_ai(session, asset_id, force=force, dry_run=dry_run)
    return {
        "id": asset.id,
        "asset_uid": asset.asset_uid,
        "ai_enrichment_status": asset.ai_enrichment_status,
        "ai_enrichment_confidence": asset.ai_enrichment_confidence,
        "needs_human_review": asset.needs_human_review,
        "review_reason": asset.review_reason,
    }


def eligible_base_filter():
    return and_(
        Asset.status.in_(("active", "ready")),
        or_(
            Asset.source_status.is_(None),
            Asset.source_status.not_in(("missing", "inaccessible", "deleted")),
        ),
        Asset.usage_scope != "restricted",
        Asset.auto_select_enabled.is_(True),
        Asset.rights_status != "restricted",
    )


def usable_production_asset_filter():
    return and_(
        Asset.status.in_(("ready", "moved")),
        Asset.move_status == "moved",
        or_(
            Asset.source_status.is_(None),
            Asset.source_status.not_in(("missing", "inaccessible", "deleted")),
        ),
        Asset.usage_scope != "restricted",
        Asset.auto_select_enabled.is_(True),
        Asset.rights_status != "restricted",
    )


def source_policy_filter(source_policy: AssetSourcePolicy):
    filters = []
    for source in source_policy.sources:
        if source.scope == "generic":
            filters.append(Asset.scope == "generic")
        elif source.scope == "brand":
            filters.append(and_(Asset.scope == "brand", Asset.brand.has(Brand.slug == source.brand)))
        elif source.scope == "title":
            filters.append(and_(Asset.scope == "title", Asset.title_slug == source.title))
    return or_(*filters) if filters else false()


def json_text_search_filter(q: str | None):
    raw_query = clean_json_value(q)
    if not raw_query:
        return None
    tokens = tokenize_query(raw_query)
    if not tokens:
        return None
    token_filters = []
    for token in tokens:
        like = f"%{token}%"
        token_filters.append(
            or_(
                Asset.filename.ilike(like),
                Asset.original_name.ilike(like),
                Asset.target_name.ilike(like),
                Asset.title.ilike(like),
                Asset.title_name.ilike(like),
                Asset.description.ilike(like),
                Asset.visual_description.ilike(like),
                Asset.action_description.ilike(like),
                Asset.primary_theme.ilike(like),
                Asset.primary_topic.ilike(like),
                Asset.search_text.ilike(like),
                Asset.embedding_text.ilike(like),
                Asset.brand.has(or_(Brand.slug.ilike(like), Brand.name.ilike(like))),
                Asset.product.has(or_(Product.slug.ilike(like), Product.name.ilike(like))),
                Asset.tags.any(AssetTag.tag.ilike(like)),
                Asset.keywords.any(AssetKeyword.keyword.ilike(like)),
                Asset.ai_analyses.any(cast(AssetAIAnalysis.result_json, String).ilike(like)),
            )
        )
    return and_(*token_filters)


def allowed_brand_exists(brand: Brand):
    return exists(
        select(AssetAllowedBrand.asset_id).where(
            AssetAllowedBrand.asset_id == Asset.id,
            AssetAllowedBrand.brand_id == brand.id,
        )
    )


def serialize_money_printer_asset(asset: Asset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_uid,
        "asset_uid": asset.asset_uid,
        "drive_file_id": asset.drive_file_id,
        "scope": asset.scope,
        "brand": asset.brand.slug if asset.scope == "brand" and asset.brand else None,
        "collection": asset.collection,
        "title_type": asset.title_type if asset.scope == "title" else None,
        "title": asset.title_name if asset.scope == "title" else None,
        "title_context": asset.title,
        "filename": asset.filename,
        "target_path": asset.remote_path,
        "media_type": asset.type,
        "orientation": asset.orientation,
        "primary_theme": asset.primary_theme,
        "primary_topic": asset.primary_topic,
        "tags": [tag.tag for tag in asset.tags],
    }


def serialize_asset(asset: Asset, score: float | None = None) -> dict[str, Any]:
    return {
        "id": asset.id,
        "score": round(score, 4) if score is not None else None,
        "asset_uid": asset.asset_uid,
        "filename": asset.filename,
        "remote_path": asset.remote_path,
        "drive_file_id": asset.drive_file_id,
        "source_path": asset.source_path,
        "type": asset.type,
        "scope": asset.scope,
        "title": {
            "type": asset.title_type,
            "name": asset.title_name,
            "slug": asset.title_slug,
            "season": asset.season_number,
            "episode": asset.episode_number,
        }
        if asset.scope == "title"
        else None,
        "description": asset.description,
        "tags": [tag.tag for tag in asset.tags],
        "suggested_uses": asset.suggested_uses or [],
        "primary_theme": asset.primary_theme,
        "primary_topic": asset.primary_topic,
        "orientation": asset.orientation,
        "width": asset.width,
        "height": asset.height,
        "duration": asset.duration_seconds,
        "can_flip_horizontal": asset.flip_horizontal_allowed,
        "can_zoom": asset.zoom_allowed,
        "generic_compatibility": asset.generic_compatibility,
        "preview_url": preview_public_url(asset.preview_path),
        "drive_location": {
            "parent_id": asset.target_parent_id,
            "name": asset.target_name or asset.filename,
            "path": asset.remote_path,
            "move_status": asset.move_status,
        },
        "status": asset.status,
        "usage_scope": asset.usage_scope,
        "rights_status": asset.rights_status,
        "auto_select_enabled": asset.auto_select_enabled,
        "quality_score": asset.quality_score,
        "usage_count": asset.usage_count,
        "last_used_at": asset.last_used_at.isoformat() if asset.last_used_at else None,
        "brand": serialize_brand(asset.brand),
        "product": serialize_product(asset.product),
        "niches": [{"slug": niche.slug, "name": niche.name} for niche in asset.niches],
        "keywords": [
            {
                "keyword": keyword.keyword,
                "category": keyword.category,
                "weight": keyword.weight,
                "confidence": keyword.confidence,
                "source": keyword.source,
                "language": keyword.language,
            }
            for keyword in sorted(asset.keywords, key=lambda item: (-item.weight, item.keyword))
        ],
        "allowed_brands": [
            {"slug": allowed.brand.slug, "name": allowed.brand.name}
            for allowed in asset.allowed_brands
            if allowed.brand is not None
        ],
    }


def serialize_brand(brand: Brand | None) -> dict[str, str] | None:
    if brand is None:
        return None
    return {"slug": brand.slug, "name": brand.name}


def serialize_product(product: Product | None) -> dict[str, str] | None:
    if product is None:
        return None
    return {"slug": product.slug, "name": product.name}
