import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db_session
from app.models import Asset, AssetAllowedBrand, AssetKeyword, Brand, Niche, Product
from app.models.asset import ORIENTATION_VALUES, USAGE_SCOPE_VALUES
from app.services.asset_policy import is_asset_eligible_for_search, resolve_asset_search_policy

router = APIRouter(prefix="/api/assets", tags=["assets"])


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

    if product_slug and not brand_slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="product_slug requires brand_slug",
        )

    filters = [eligible_base_filter()]
    brand: Brand | None = None
    product: Product | None = None

    if brand_slug:
        brand = session.scalar(
            select(Brand)
            .where(Brand.slug == brand_slug)
            .options(selectinload(Brand.asset_policy))
        )
        if brand is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand not found: {brand_slug}",
            )
        filters.append(
            or_(
                Asset.brand_id == brand.id,
                Asset.usage_scope == "global",
                allowed_brand_exists(brand),
            )
        )
    if product_slug and brand is not None:
        product = session.scalar(
            select(Product)
            .where(Product.brand_id == brand.id, Product.slug == product_slug)
            .options(selectinload(Product.asset_policy))
        )
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product not found: {product_slug}",
            )

    if brand is None and include_global_assets is False:
        filters.append(Asset.usage_scope != "global")

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

    q_value = clean_search_query(q)
    keyword_score = literal(0)
    text_score = literal(0)
    if q_value:
        like = f"%{q_value}%"
        keyword_match = exists(
            select(AssetKeyword.asset_id).where(
                AssetKeyword.asset_id == Asset.id,
                AssetKeyword.keyword.ilike(like),
            )
        )
        text_match = or_(
            Asset.search_text.ilike(like),
            Asset.title.ilike(like),
            Asset.filename.ilike(like),
            Asset.remote_path.ilike(like),
        )
        filters.append(or_(keyword_match, text_match))
        keyword_score = case((keyword_match, 2), else_=0)
        text_score = case((text_match, 1), else_=0)

    query = (
        select(Asset)
        .where(and_(*filters))
        .options(
            selectinload(Asset.source),
            selectinload(Asset.brand),
            selectinload(Asset.product),
            selectinload(Asset.niches),
            selectinload(Asset.keywords),
            selectinload(Asset.allowed_brands).selectinload(AssetAllowedBrand.brand),
        )
        .order_by(
            keyword_score.desc(),
            text_score.desc(),
            func.coalesce(Asset.quality_score, 0).desc(),
            Asset.usage_count.asc(),
            case((Asset.last_used_at.is_(None), 0), else_=1).asc(),
            Asset.last_used_at.asc(),
            Asset.id.asc(),
        )
        .limit(max(limit * 20, 200))
    )
    policy = resolve_asset_search_policy(brand=brand, product=product)
    assets = [
        asset
        for asset in session.scalars(query).all()
        if is_asset_eligible_for_search(
            asset=asset,
            brand=brand,
            product=product,
            include_global_assets=include_global_assets,
            include_stock_assets=include_stock_assets,
            policy=policy,
        )
    ][:limit]

    return {
        "count": len(assets),
        "limit": limit,
        "assets": [serialize_asset(asset) for asset in assets],
    }


def eligible_base_filter():
    return and_(
        Asset.status == "active",
        Asset.usage_scope != "restricted",
        Asset.auto_select_enabled.is_(True),
        Asset.rights_status != "restricted",
    )


def allowed_brand_exists(brand: Brand):
    return exists(
        select(AssetAllowedBrand.asset_id).where(
            AssetAllowedBrand.asset_id == Asset.id,
            AssetAllowedBrand.brand_id == brand.id,
        )
    )


def clean_search_query(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def serialize_asset(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "asset_uid": asset.asset_uid,
        "filename": asset.filename,
        "remote_path": asset.remote_path,
        "source_path": asset.source_path,
        "type": asset.type,
        "orientation": asset.orientation,
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
