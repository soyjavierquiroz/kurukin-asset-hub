from collections import defaultdict
from datetime import UTC, datetime
import logging
import math
import mimetypes
import secrets
import shutil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, String, and_, cast, delete, distinct, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db_session
from app.models import (
    Asset,
    AssetAllowedBrand,
    AssetAIAnalysis,
    AssetCollection,
    AssetKeyword,
    AssetNiche,
    AssetSegment,
    AssetSegmentationRun,
    AssetTag,
    Brand,
    JobAssetBundleItem,
    Niche,
    Product,
    Source,
    SourceSyncRun,
)
from app.models.asset import (
    AI_ENRICHMENT_STATUS_VALUES,
    ASSET_STATUS_VALUES,
    ASSET_TYPE_VALUES,
    MOVE_STATUS_VALUES,
    ORIENTATION_VALUES,
    USAGE_SCOPE_VALUES,
)
from app.models.common import PROVIDER_VALUES
from app.services.asset_preview import (
    PREVIEW_FILENAMES,
    generate_asset_preview,
    local_preview_file,
    preview_public_url,
    preview_output_dir,
    safe_asset_uid,
)
from app.services.ai_asset_enrichment import enrich_asset_with_ai
from app.services.managed_drive_pilot import (
    ReviewApproval,
    approve_review_asset,
    drive_client_from_environment,
    mutation_client_for_apply,
    review_approval_has_meaningful_naming_source,
    update_reviewed_asset_metadata,
)
from app.services.long_video_segmentation import (
    approve_segmentation,
    delete_original_after_approval,
    segment_long_video_asset,
)
from app.services.source_sync import scan_source

router = APIRouter(tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()
logger = logging.getLogger(__name__)


def require_admin(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    settings = get_settings()
    username_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


AdminUser = Annotated[str, Depends(require_admin)]
DbSession = Annotated[Session, Depends(get_db_session)]


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def clean_bool(value: str | None) -> bool:
    return value == "on"


def clean_int(value: str | None) -> int | None:
    if not value:
        return None
    return int(value)


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def build_query_string(params: dict[str, object]) -> str:
    clean_params = {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in params.items()
        if value not in (None, "")
    }
    return urlencode(clean_params)


def bool_filter(value: str | bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def latest_ai_result(asset: Asset) -> dict[str, object]:
    analyses = sorted(
        asset.ai_analyses,
        key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.id or 0),
        reverse=True,
    )
    if not analyses:
        return {}
    result = analyses[0].result_json
    return result if isinstance(result, dict) else {}


def latest_ai_results(assets: list[Asset]) -> dict[int, dict[str, object]]:
    return {asset.id: latest_ai_result(asset) for asset in assets}


def latest_ai_value(field: str):
    return AssetAIAnalysis.result_json[field].as_string()


def pending_review_filter():
    return or_(Asset.status == "review_required", Asset.needs_human_review.is_(True))


VISUAL_PRESENTATION_VALUES = ("masculine", "feminine", "mixed", "unclear", "not_applicable")
PERSON_VISIBILITY_VALUES = (
    "clear",
    "partial",
    "back_view",
    "silhouette",
    "occluded",
    "not_applicable",
)


def review_approval_from_form(
    file_id: str,
    reviewed_by: str,
    primary_theme: str | None,
    primary_topic: str | None,
    tags: str | None,
    visual_presentation: str,
    visual_presentation_confidence: float,
    person_visibility: str,
    people_count: int | None,
) -> ReviewApproval:
    return ReviewApproval(
        file_id=file_id,
        primary_theme=clean_text(primary_theme),
        primary_topic=clean_text(primary_topic),
        tags=tuple(split.strip() for split in (tags or "").split(",") if split.strip()),
        reviewed_by=reviewed_by,
        visual_presentation=visual_presentation,
        visual_presentation_confidence=visual_presentation_confidence,
        person_visibility=person_visibility,
        people_count=people_count,
    )


def review_form_values(
    ai_result: dict[str, object],
    asset: Asset,
    *,
    primary_theme: str | None = None,
    primary_topic: str | None = None,
    tags: str | None = None,
    visual_presentation: str | None = None,
    visual_presentation_confidence: float | None = None,
    person_visibility: str | None = None,
    people_count: int | None = None,
) -> dict[str, object]:
    return {
        "visual_presentation": visual_presentation or str(ai_result.get("visual_presentation") or "not_applicable"),
        "visual_presentation_confidence": (
            visual_presentation_confidence
            if visual_presentation_confidence is not None
            else ai_result.get("visual_presentation_confidence", 1.0)
        ),
        "person_visibility": person_visibility or str(ai_result.get("person_visibility") or "not_applicable"),
        "people_count": people_count if people_count is not None else ai_result.get("people_count"),
        "primary_theme": primary_theme if primary_theme is not None else asset.primary_theme or ai_result.get("primary_theme", ""),
        "primary_topic": primary_topic if primary_topic is not None else asset.primary_topic or ai_result.get("primary_topic", ""),
        "tags": tags if tags is not None else ", ".join(str(tag) for tag in ai_result.get("tags", []) or []),
    }


def review_query_base() -> Select[tuple[Asset]]:
    return select(Asset).where(pending_review_filter())


def next_pending_review_id(session: Session) -> int | None:
    return session.scalar(
        select(Asset.id).where(pending_review_filter()).order_by(Asset.created_at.asc(), Asset.id.asc()).limit(1)
    )


def preview_directory_for_discard(asset_id: int, preview_paths: list[str | None]) -> Path:
    root = Path(get_settings().pilot_preview_root).resolve()
    candidates: list[Path] = []
    for preview_path in preview_paths:
        local_path = local_preview_file(preview_path)
        if local_path is None:
            continue
        try:
            resolved = local_path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        candidates.append(resolved.parent)
    if candidates:
        return candidates[0]
    return root / str(asset_id)


def cleanup_discard_preview_dir(asset_id: int, preview_paths: list[str | None]) -> None:
    root = Path(get_settings().pilot_preview_root).resolve()
    preview_dir = preview_directory_for_discard(asset_id, preview_paths).resolve(strict=False)
    try:
        preview_dir.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("preview cleanup path escaped PILOT_PREVIEW_ROOT") from exc
    if preview_dir == root:
        raise RuntimeError("preview cleanup refused PILOT_PREVIEW_ROOT")
    if not preview_dir.exists():
        return
    shutil.rmtree(preview_dir, ignore_errors=False)


def delete_asset_with_dependents(session: Session, asset: Asset) -> None:
    asset_id = asset.id
    session.execute(delete(AssetNiche).where(AssetNiche.asset_id == asset_id))
    session.execute(delete(AssetCollection).where(AssetCollection.asset_id == asset_id))
    session.execute(update(JobAssetBundleItem).where(JobAssetBundleItem.asset_id == asset_id).values(asset_id=None))
    session.execute(update(AssetSegment).where(AssetSegment.child_asset_id == asset_id).values(child_asset_id=None))
    run_ids = session.scalars(
        select(AssetSegmentationRun.id).where(AssetSegmentationRun.parent_asset_id == asset_id)
    ).all()
    if run_ids:
        session.execute(delete(AssetSegment).where(AssetSegment.run_id.in_(run_ids)))
        session.execute(delete(AssetSegmentationRun).where(AssetSegmentationRun.id.in_(run_ids)))
    session.execute(delete(AssetSegment).where(AssetSegment.parent_asset_id == asset_id))
    session.execute(update(Asset).where(Asset.parent_asset_id == asset_id).values(parent_asset_id=None))
    session.delete(asset)


def discard_asset_from_catalog(session: Session, asset: Asset) -> None:
    asset_id = asset.id
    file_id = asset.drive_file_id
    if not file_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Drive file ID")

    preview_paths = [asset.thumbnail_path, asset.preview_path]
    read_client = drive_client_from_environment()
    drive_client = mutation_client_for_apply(read_client)
    try:
        drive_client.trash_file(file_id)
        metadata = drive_client.get_file_metadata(file_id)
    except Exception as exc:
        logger.exception("Drive trash failed for asset_id=%s file_id=%s", asset_id, file_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Drive trash failed: {exc}",
        ) from exc
    if metadata.id != file_id or not metadata.trashed:
        logger.error(
            "Drive trash verification failed for asset_id=%s file_id=%s trashed=%s",
            asset_id,
            file_id,
            metadata.trashed,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Drive trash verification failed",
        )

    try:
        delete_asset_with_dependents(session, asset)
        session.commit()
    except Exception as exc:
        session.rollback()
        try:
            drive_client.untrash_file(file_id)
        except Exception:
            logger.exception("Drive untrash rollback failed for asset_id=%s file_id=%s", asset_id, file_id)
        logger.exception("Database discard failed after Drive trash for asset_id=%s file_id=%s", asset_id, file_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database discard failed after Drive trash; Drive untrash rollback was attempted",
        ) from exc

    try:
        cleanup_discard_preview_dir(asset_id, preview_paths)
    except Exception:
        logger.warning("Preview cleanup failed for discarded asset_id=%s", asset_id, exc_info=True)


def asset_sort_order(sort: str | None) -> list[object]:
    match sort:
        case "oldest":
            return [Asset.created_at.asc(), Asset.id.asc()]
        case "filename":
            return [Asset.filename.asc(), Asset.id.asc()]
        case "ai_confidence":
            return [Asset.ai_enrichment_confidence.desc().nullslast(), Asset.created_at.desc(), Asset.id.desc()]
        case "duration":
            return [Asset.duration_seconds.desc().nullslast(), Asset.created_at.desc(), Asset.id.desc()]
        case "recently_reviewed":
            return [Asset.reviewed_at.desc().nullslast(), Asset.created_at.desc(), Asset.id.desc()]
        case _:
            return [Asset.created_at.desc(), Asset.id.desc()]


def apply_asset_filters(
    query: Select[tuple[Asset]],
    filters: list[object],
    *,
    niche_id: int | None = None,
    niche: str | None = None,
) -> Select[tuple[Asset]]:
    if niche_id:
        query = query.join(Asset.niches).where(Niche.id == niche_id)
    if clean_text(niche):
        query = query.join(Asset.niches).where(or_(Niche.slug == niche.strip(), Niche.name == niche.strip()))
    if filters:
        query = query.where(and_(*filters))
    return query


def build_asset_filters(
    *,
    q: str | None,
    brand_id: int | None = None,
    product_id: int | None = None,
    type: str | None = None,
    orientation: str | None = None,
    status: str | None = None,
    move_status: str | None = None,
    brand: str | None = None,
    title_type: str | None = None,
    title_slug: str | None = None,
    keyword: str | None = None,
    usage_scope: str | None = None,
    scope: str | None = None,
    auto_select_enabled: bool | None = None,
    ai_status: str | None = None,
    needs_review: bool | None = None,
    primary_theme: str | None = None,
    contains_people: bool | None = None,
    visual_presentation: str | None = None,
    person_visibility: str | None = None,
) -> list[object]:
    filters: list[object] = []
    if clean_text(q):
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Asset.asset_uid.ilike(like),
                Asset.filename.ilike(like),
                Asset.remote_path.ilike(like),
                Asset.source_path.ilike(like),
                Asset.target_name.ilike(like),
                Asset.title.ilike(like),
                Asset.title_name.ilike(like),
                Asset.description.ilike(like),
                Asset.visual_description.ilike(like),
                Asset.action_description.ilike(like),
                Asset.primary_theme.ilike(like),
                Asset.primary_topic.ilike(like),
                Asset.search_text.ilike(like),
                Asset.scope.ilike(like),
                Asset.people.ilike(like),
                Asset.drive_file_id.ilike(like),
                Asset.brand.has(Brand.name.ilike(like)),
                Asset.product.has(Product.name.ilike(like)),
                Asset.tags.any(AssetTag.tag.ilike(like)),
                Asset.keywords.any(AssetKeyword.keyword.ilike(like)),
                Asset.ai_analyses.any(cast(AssetAIAnalysis.result_json, String).ilike(like)),
            )
        )
    if brand_id:
        filters.append(Asset.brand_id == brand_id)
    if clean_text(brand):
        filters.append(Asset.brand.has(Brand.slug == brand.strip()))
    if product_id:
        filters.append(Asset.product_id == product_id)
    if type:
        filters.append(Asset.type == type)
    if orientation:
        filters.append(Asset.orientation == orientation)
    if status:
        filters.append(Asset.status == status)
    if move_status:
        filters.append(Asset.move_status == move_status)
    if clean_text(keyword):
        filters.append(Asset.keywords.any(AssetKeyword.keyword.ilike(f"%{keyword.strip()}%")))
    if usage_scope:
        filters.append(Asset.usage_scope == usage_scope)
    if scope:
        filters.append(Asset.scope == scope)
    if title_type:
        filters.append(Asset.title_type == title_type)
    if clean_text(title_slug):
        filters.append(Asset.title_slug == title_slug.strip())
    if auto_select_enabled is not None:
        filters.append(Asset.auto_select_enabled.is_(auto_select_enabled))
    if ai_status:
        filters.append(Asset.ai_enrichment_status == ai_status)
    if needs_review is not None:
        filters.append(Asset.needs_human_review.is_(needs_review))
    if primary_theme:
        filters.append(Asset.primary_theme == primary_theme)
    if contains_people is not None:
        filters.append(
            Asset.ai_analyses.any(AssetAIAnalysis.result_json["contains_people"].as_boolean().is_(contains_people))
        )
    if visual_presentation:
        filters.append(Asset.ai_analyses.any(latest_ai_value("visual_presentation") == visual_presentation))
    if person_visibility:
        filters.append(Asset.ai_analyses.any(latest_ai_value("person_visibility") == person_visibility))
    return filters


def get_or_404(session: Session, model: type[Brand] | type[Product] | type[Niche] | type[Source], item_id: int):
    item = session.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return item


@router.get("/")
def dashboard(request: Request, _: AdminUser, session: DbSession):
    counts = {
        "assets": session.scalar(select(func.count()).select_from(Asset)) or 0,
        "sources": session.scalar(select(func.count()).select_from(Source)) or 0,
        "brands": session.scalar(select(func.count()).select_from(Brand)) or 0,
        "products": session.scalar(select(func.count()).select_from(Product)) or 0,
        "niches": session.scalar(select(func.count()).select_from(Niche)) or 0,
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"page_title": "Dashboard", "counts": counts},
    )


@router.get("/assets")
def assets_index(
    request: Request,
    _: AdminUser,
    session: DbSession,
    q: str | None = None,
    brand_id: int | None = None,
    brand: str | None = None,
    product_id: int | None = None,
    niche_id: int | None = None,
    niche: str | None = None,
    type: str | None = None,
    media_type: str | None = None,
    orientation: str | None = None,
    status: str | None = None,
    move_status: str | None = None,
    scope: str | None = None,
    title_type: str | None = None,
    title_slug: str | None = None,
    primary_theme: str | None = None,
    contains_people: str | None = None,
    visual_presentation: str | None = None,
    person_visibility: str | None = None,
    keyword: str | None = None,
    usage_scope: str | None = None,
    auto_select_enabled: str | None = None,
    ai_status: str | None = None,
    needs_review: str | None = None,
    needs_human_review: str | None = None,
    sort: str = "newest",
    page: int = 1,
    discarded: bool = False,
):
    page = max(page, 1)
    per_page = 48
    auto_select_filter = bool_filter(auto_select_enabled)
    human_review_filter = bool_filter(needs_human_review)
    legacy_review_filter = bool_filter(needs_review)
    review_filter = human_review_filter if human_review_filter is not None else legacy_review_filter
    people_filter = bool_filter(contains_people)
    filters = build_asset_filters(
        q=q,
        brand_id=brand_id,
        product_id=product_id,
        type=media_type or type,
        orientation=orientation,
        status=status,
        move_status=move_status,
        brand=brand,
        title_type=title_type,
        title_slug=title_slug,
        keyword=keyword,
        usage_scope=usage_scope,
        scope=scope,
        auto_select_enabled=auto_select_filter,
        ai_status=ai_status,
        needs_review=review_filter,
        primary_theme=primary_theme,
        contains_people=people_filter,
        visual_presentation=visual_presentation,
        person_visibility=person_visibility,
    )

    query: Select[tuple[Asset]] = select(Asset).options(
        selectinload(Asset.source),
        selectinload(Asset.brand),
        selectinload(Asset.product),
        selectinload(Asset.niches),
        selectinload(Asset.keywords),
        selectinload(Asset.ai_analyses),
    )
    count_query = select(func.count(func.distinct(Asset.id))).select_from(Asset)
    query = apply_asset_filters(query, filters, niche_id=niche_id, niche=niche)
    count_query = apply_asset_filters(count_query, filters, niche_id=niche_id, niche=niche)

    total = session.scalar(count_query) or 0
    catalog_total = session.scalar(select(func.count()).select_from(Asset)) or 0
    needs_review_count = session.scalar(
        select(func.count()).select_from(Asset).where(pending_review_filter())
    ) or 0
    assets = session.scalars(
        query.order_by(*asset_sort_order(sort))
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    pages = max(math.ceil(total / per_page), 1)

    return templates.TemplateResponse(
        request,
        "assets/index.html",
        {
            "page_title": "Assets",
            "assets": assets,
            "brands": session.scalars(
                select(Brand)
                .join(Asset)
                .where(Asset.scope == "brand")
                .distinct()
                .order_by(Brand.name)
            ).all(),
            "products": session.scalars(select(Product).order_by(Product.name)).all(),
            "niches": session.scalars(
                select(Niche)
                .join(AssetNiche, AssetNiche.niche_id == Niche.id)
                .distinct()
                .order_by(Niche.name)
            ).all(),
            "title_type_values": [
                value
                for value in session.scalars(
                    select(Asset.title_type).where(Asset.title_type.is_not(None)).distinct().order_by(Asset.title_type)
                ).all()
                if value
            ],
            "title_slug_values": session.scalars(
                select(Asset.title_slug).where(Asset.title_slug.is_not(None)).distinct().order_by(Asset.title_slug)
            ).all(),
            "filters": {
                "q": q or "",
                "brand_id": brand_id,
                "brand": brand or "",
                "product_id": product_id,
                "niche_id": niche_id,
                "niche": niche or "",
                "type": media_type or type or "",
                "media_type": media_type or type or "",
                "orientation": orientation or "",
                "status": status or "",
                "move_status": move_status or "",
                "scope": scope or "",
                "title_type": title_type or "",
                "title_slug": title_slug or "",
                "primary_theme": primary_theme or "",
                "contains_people": contains_people or "",
                "visual_presentation": visual_presentation or "",
                "person_visibility": person_visibility or "",
                "keyword": keyword or "",
                "usage_scope": usage_scope or "",
                "auto_select_enabled": auto_select_filter,
                "ai_status": ai_status or "",
                "needs_review": review_filter,
                "needs_human_review": review_filter,
                "sort": sort,
            },
            "type_values": ASSET_TYPE_VALUES,
            "orientation_values": ORIENTATION_VALUES,
            "status_values": ASSET_STATUS_VALUES,
            "move_status_values": MOVE_STATUS_VALUES,
            "scope_values": ("generic", "brand", "title"),
            "primary_theme_values": sorted(
                value
                for value in session.scalars(
                    select(Asset.primary_theme).where(Asset.primary_theme.is_not(None)).distinct()
                ).all()
                if value
            ),
            "visual_presentation_values": ("masculine", "feminine", "mixed", "unclear", "not_applicable"),
            "person_visibility_values": (
                "clear",
                "partial",
                "back_view",
                "silhouette",
                "occluded",
                "not_applicable",
            ),
            "usage_scope_values": USAGE_SCOPE_VALUES,
            "ai_status_values": AI_ENRICHMENT_STATUS_VALUES,
            "pagination_query": build_query_string(
                {
                    "q": q,
                    "brand_id": brand_id,
                    "brand": brand,
                    "product_id": product_id,
                    "niche_id": niche_id,
                    "niche": niche,
                    "media_type": media_type or type,
                    "orientation": orientation,
                    "status": status,
                    "move_status": move_status,
                    "scope": scope,
                    "title_type": title_type,
                    "title_slug": title_slug,
                    "primary_theme": primary_theme,
                    "contains_people": contains_people,
                    "visual_presentation": visual_presentation,
                    "person_visibility": person_visibility,
                    "keyword": keyword,
                    "usage_scope": usage_scope,
                    "auto_select_enabled": auto_select_filter,
                    "ai_status": ai_status,
                    "needs_human_review": review_filter,
                    "sort": sort,
                }
            ),
            "discarded": discarded,
            "page": page,
            "pages": pages,
            "total": total,
            "catalog_total": catalog_total,
            "needs_review_count": needs_review_count,
            "ai_results": latest_ai_results(assets),
            "long_video_threshold_seconds": get_settings().long_video_threshold_seconds,
            "preview_public_url": preview_public_url,
        },
    )


@router.get("/assets/{asset_id}")
def assets_detail(request: Request, asset_id: int, _: AdminUser, session: DbSession):
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(
            selectinload(Asset.source),
            selectinload(Asset.brand),
            selectinload(Asset.product),
            selectinload(Asset.niches),
            selectinload(Asset.tags),
            selectinload(Asset.keywords),
            selectinload(Asset.ai_analyses),
            selectinload(Asset.allowed_brands).selectinload(
                AssetAllowedBrand.brand,
            ),
            selectinload(Asset.usages),
            selectinload(Asset.collections),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    settings = get_settings()
    derived_clips = session.scalars(
        select(Asset)
        .where(Asset.parent_asset_id == asset.id)
        .order_by(Asset.segment_index.asc(), Asset.id.asc())
    ).all()
    ai_keywords_by_category: dict[str, list[AssetKeyword]] = defaultdict(list)
    for keyword in sorted(
        (keyword for keyword in asset.keywords if keyword.source == "ai"),
        key=lambda item: (item.category, -item.weight, item.keyword),
    ):
        ai_keywords_by_category[keyword.category].append(keyword)
    return templates.TemplateResponse(
        request,
        "assets/detail.html",
        {
            "page_title": asset.filename,
            "asset": asset,
            "metadata_status": request.query_params.get("metadata_status"),
            "derived_clips": derived_clips,
            "long_video_threshold_seconds": settings.long_video_threshold_seconds,
            "ai_keywords_by_category": dict(ai_keywords_by_category),
            "ai_result": latest_ai_result(asset),
            "preview_public_url": preview_public_url,
        },
    )


@router.get("/assets/{asset_id}/edit-metadata")
def assets_edit_metadata(request: Request, asset_id: int, _: AdminUser, session: DbSession):
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.ai_analyses))
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    ai_result = latest_ai_result(asset)
    return templates.TemplateResponse(
        request,
        "assets/edit_metadata.html",
        {
            "page_title": f"Editar metadata {asset.filename}",
            "asset": asset,
            "ai_result": ai_result,
            "form_values": review_form_values(ai_result, asset),
            "validation_error": None,
            "visual_presentation_values": VISUAL_PRESENTATION_VALUES,
            "person_visibility_values": PERSON_VISIBILITY_VALUES,
        },
    )


@router.post("/assets/{asset_id}/edit-metadata")
def assets_update_metadata(
    request: Request,
    asset_id: int,
    admin_user: AdminUser,
    session: DbSession,
    visual_presentation: Annotated[str, Form()],
    visual_presentation_confidence: Annotated[float, Form()],
    person_visibility: Annotated[str, Form()],
    people_count: Annotated[int | None, Form()] = None,
    primary_theme: Annotated[str | None, Form()] = None,
    primary_topic: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
):
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.ai_analyses))
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if not asset.drive_file_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Drive file ID")
    approval = review_approval_from_form(
        asset.drive_file_id,
        admin_user,
        primary_theme,
        primary_topic,
        tags,
        visual_presentation,
        visual_presentation_confidence,
        person_visibility,
        people_count,
    )
    if not review_approval_has_meaningful_naming_source(asset, approval):
        ai_result = latest_ai_result(asset)
        return templates.TemplateResponse(
            request,
            "assets/edit_metadata.html",
            {
                "page_title": f"Editar metadata {asset.filename}",
                "asset": asset,
                "ai_result": ai_result,
                "form_values": review_form_values(
                    ai_result,
                    asset,
                    primary_theme=primary_theme,
                    primary_topic=primary_topic,
                    tags=tags,
                    visual_presentation=visual_presentation,
                    visual_presentation_confidence=visual_presentation_confidence,
                    person_visibility=person_visibility,
                    people_count=people_count,
                ),
                "validation_error": "Indica un Primary topic descriptivo para poder aprobar este asset.",
                "visual_presentation_values": VISUAL_PRESENTATION_VALUES,
                "person_visibility_values": PERSON_VISIBILITY_VALUES,
            },
        )
    result = update_reviewed_asset_metadata(session, asset, approval)
    if asset.move_status == "planned":
        status_value = "planned_recalculated"
    elif asset.move_status == "moved" and result is not None and result.changed:
        status_value = "rename_pending"
    else:
        status_value = "updated"
    return redirect_to(f"/assets/{asset_id}?metadata_status={status_value}")


@router.post("/assets/{asset_id}/segment")
def assets_segment_video(asset_id: int, admin_user: AdminUser, session: DbSession):
    segment_long_video_asset(session, asset_id, force=False, created_by=admin_user)
    return redirect_to(f"/assets/{asset_id}")


@router.post("/assets/{asset_id}/approve-segmentation")
def assets_approve_segmentation(asset_id: int, admin_user: AdminUser, session: DbSession):
    approve_segmentation(session, asset_id, approved_by=admin_user)
    return redirect_to(f"/assets/{asset_id}")


@router.post("/assets/{asset_id}/delete-original")
def assets_delete_original(asset_id: int, _: AdminUser, session: DbSession):
    delete_original_after_approval(session, asset_id, force=False)
    return redirect_to(f"/assets/{asset_id}")


@router.post("/assets/{asset_id}/generate-preview")
def assets_generate_preview(asset_id: int, _: AdminUser, session: DbSession):
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    force = asset.preview_status == "ready"
    generate_asset_preview(session, asset_id, force=force)
    return redirect_to(f"/assets/{asset_id}")


@router.post("/assets/{asset_id}/ai-enrich")
def assets_ai_enrich(
    asset_id: int,
    _: AdminUser,
    session: DbSession,
    force: Annotated[str | None, Form()] = None,
    dry_run: Annotated[str | None, Form()] = None,
):
    enrich_asset_with_ai(
        session,
        asset_id,
        force=force in {"true", "on"},
        dry_run=dry_run in {"true", "on"},
    )
    return redirect_to(f"/assets/{asset_id}")


@router.post("/assets/{asset_id}/discard")
def assets_discard(asset_id: int, _: AdminUser, session: DbSession):
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    discard_asset_from_catalog(session, asset)
    return redirect_to("/assets?discarded=true")


@router.post("/assets/generate-previews-bulk")
async def assets_generate_previews_bulk(request: Request, _: AdminUser, session: DbSession):
    form = await request.form()
    raw_ids = form.getlist("asset_ids")
    force = form.get("force") == "on"
    for raw_id in raw_ids:
        try:
            generate_asset_preview(session, int(raw_id), force=force)
        except (TypeError, ValueError):
            continue
    return redirect_to("/assets")


@router.post("/assets/ai-enrich-bulk")
async def assets_ai_enrich_bulk(request: Request, _: AdminUser, session: DbSession):
    form = await request.form()
    raw_ids = form.getlist("asset_ids")
    force = form.get("force") == "on"
    for raw_id in raw_ids:
        try:
            enrich_asset_with_ai(session, int(raw_id), force=force)
        except (TypeError, ValueError):
            continue
    return redirect_to("/assets")


@router.get("/reviews")
def reviews_index(
    request: Request,
    _: AdminUser,
    session: DbSession,
    q: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    type: str | None = None,
    primary_theme: str | None = None,
    orientation: str | None = None,
    contains_people: str | None = None,
    visual_presentation: str | None = None,
    person_visibility: str | None = None,
    page: int = 1,
    completed: bool = False,
):
    page = max(page, 1)
    per_page = 24
    filters = build_asset_filters(
        q=q,
        type=type,
        orientation=orientation,
        status=status,
        scope=scope,
        primary_theme=primary_theme,
        contains_people=bool_filter(contains_people),
        visual_presentation=visual_presentation,
        person_visibility=person_visibility,
    )
    pending = pending_review_filter()
    query = (
        select(Asset)
        .where(pending)
        .options(selectinload(Asset.source), selectinload(Asset.keywords), selectinload(Asset.ai_analyses))
    )
    count_query = select(func.count()).select_from(Asset).where(pending)
    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))
    total = session.scalar(count_query) or 0
    assets = session.scalars(
        query.order_by(Asset.created_at.asc(), Asset.id.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    pages = max(math.ceil(total / per_page), 1)
    return templates.TemplateResponse(
        request,
        "reviews/index.html",
        {
            "page_title": "Revision",
            "assets": assets,
            "filters": {
                "q": q or "",
                "status": status or "",
                "scope": scope or "",
                "type": type or "",
                "primary_theme": primary_theme or "",
                "orientation": orientation or "",
                "contains_people": contains_people or "",
                "visual_presentation": visual_presentation or "",
                "person_visibility": person_visibility or "",
            },
            "status_values": ASSET_STATUS_VALUES,
            "scope_values": ("generic", "brand", "title"),
            "type_values": ASSET_TYPE_VALUES,
            "orientation_values": ORIENTATION_VALUES,
            "primary_theme_values": sorted(
                value
                for value in session.scalars(
                    select(Asset.primary_theme).where(Asset.primary_theme.is_not(None)).distinct()
                ).all()
                if value
            ),
            "visual_presentation_values": ("masculine", "feminine", "mixed", "unclear", "not_applicable"),
            "person_visibility_values": (
                "clear",
                "partial",
                "back_view",
                "silhouette",
                "occluded",
                "not_applicable",
            ),
            "pagination_query": build_query_string(
                {
                    "q": q,
                    "status": status,
                    "scope": scope,
                    "type": type,
                    "primary_theme": primary_theme,
                    "orientation": orientation,
                    "contains_people": contains_people,
                    "visual_presentation": visual_presentation,
                    "person_visibility": person_visibility,
                }
            ),
            "page": page,
            "pages": pages,
            "total": total,
            "completed": completed,
            "ai_results": latest_ai_results(assets),
            "preview_public_url": preview_public_url,
        },
    )


@router.get("/reviews/{asset_id}")
def reviews_detail(request: Request, asset_id: int, _: AdminUser, session: DbSession):
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.keywords), selectinload(Asset.ai_analyses))
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if asset.status != "review_required" and not asset.needs_human_review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    pending_count = session.scalar(select(func.count()).select_from(Asset).where(pending_review_filter())) or 0
    next_pending = session.scalar(
        select(Asset.id)
        .where(pending_review_filter(), Asset.id != asset.id)
        .order_by(Asset.created_at.asc(), Asset.id.asc())
        .limit(1)
    )
    return templates.TemplateResponse(
        request,
        "reviews/form.html",
        {
            "page_title": f"Review {asset.filename}",
            "asset": asset,
            "pending_count": pending_count,
            "next_pending_id": next_pending,
            "ai_result": latest_ai_result(asset),
            "form_values": review_form_values(latest_ai_result(asset), asset),
            "validation_error": None,
            "preview_public_url": preview_public_url,
            "visual_presentation_values": VISUAL_PRESENTATION_VALUES,
            "person_visibility_values": PERSON_VISIBILITY_VALUES,
        },
    )


@router.post("/reviews/{asset_id}")
def reviews_approve(
    request: Request,
    asset_id: int,
    admin_user: AdminUser,
    session: DbSession,
    visual_presentation: Annotated[str, Form()],
    visual_presentation_confidence: Annotated[float, Form()],
    person_visibility: Annotated[str, Form()],
    people_count: Annotated[int | None, Form()] = None,
    primary_theme: Annotated[str | None, Form()] = None,
    primary_topic: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    action: Annotated[str, Form()] = "save",
):
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.ai_analyses))
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if not asset.drive_file_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Drive file ID")
    approval = review_approval_from_form(
        asset.drive_file_id,
        admin_user,
        primary_theme,
        primary_topic,
        tags,
        visual_presentation,
        visual_presentation_confidence,
        person_visibility,
        people_count,
    )
    if not review_approval_has_meaningful_naming_source(asset, approval):
        pending_count = session.scalar(select(func.count()).select_from(Asset).where(pending_review_filter())) or 0
        next_pending = session.scalar(
            select(Asset.id)
            .where(pending_review_filter(), Asset.id != asset.id)
            .order_by(Asset.created_at.asc(), Asset.id.asc())
            .limit(1)
        )
        ai_result = latest_ai_result(asset)
        return templates.TemplateResponse(
            request,
            "reviews/form.html",
            {
                "page_title": f"Review {asset.filename}",
                "asset": asset,
                "pending_count": pending_count,
                "next_pending_id": next_pending,
                "ai_result": ai_result,
                "form_values": review_form_values(
                    ai_result,
                    asset,
                    primary_theme=primary_theme,
                    primary_topic=primary_topic,
                    tags=tags,
                    visual_presentation=visual_presentation,
                    visual_presentation_confidence=visual_presentation_confidence,
                    person_visibility=person_visibility,
                    people_count=people_count,
                ),
                "validation_error": "Indica un Primary topic descriptivo para poder aprobar este asset.",
                "preview_public_url": preview_public_url,
                "visual_presentation_values": VISUAL_PRESENTATION_VALUES,
                "person_visibility_values": PERSON_VISIBILITY_VALUES,
            },
        )
    try:
        approve_review_asset(session, approval)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if action == "save_next":
        next_asset_id = session.scalar(
            select(Asset.id)
            .where(pending_review_filter())
            .order_by(Asset.created_at.asc(), Asset.id.asc())
            .limit(1)
        )
        if next_asset_id is None:
            return redirect_to("/reviews?completed=true")
        return redirect_to(f"/reviews/{next_asset_id}")
    return redirect_to(f"/assets/{asset_id}")


@router.post("/reviews/{asset_id}/discard")
def reviews_discard(
    asset_id: int,
    _: AdminUser,
    session: DbSession,
    action: Annotated[str, Form()] = "discard",
):
    if action not in {"discard", "discard_next"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid discard action")
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if asset.status != "review_required" and not asset.needs_human_review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    discard_asset_from_catalog(session, asset)

    if action == "discard_next":
        next_asset_id = next_pending_review_id(session)
        if next_asset_id is None:
            return redirect_to("/reviews?completed=true")
        return redirect_to(f"/reviews/{next_asset_id}")
    return redirect_to("/reviews")


def serve_preview_file(asset_uid: str, filename: str) -> FileResponse:
    if filename not in PREVIEW_FILENAMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found")
    try:
        safe_uid = safe_asset_uid(asset_uid)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found") from exc
    path = preview_output_dir(safe_uid) / filename
    if not path.is_file() or Path(path).parent != preview_output_dir(safe_uid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found")
    return FileResponse(path)


def serve_media_path(path: str) -> FileResponse:
    file_path = local_preview_file(path)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found")
    normalized = path.strip().lstrip("/")
    settings = get_settings()
    if normalized.startswith("pilot-previews/"):
        root = Path(settings.pilot_preview_root).resolve()
    else:
        root = Path(settings.preview_storage_dir).resolve()
    try:
        resolved = file_path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found") from exc
    media_type = mimetypes.guess_type(resolved.name)[0]
    if resolved.suffix == ".webp":
        media_type = "image/webp"
    return FileResponse(resolved, media_type=media_type)


@router.get("/media/previews/{asset_uid}/thumbnail.jpg")
def media_preview_thumbnail(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "thumbnail.jpg")


@router.get("/media/previews/{asset_uid}/preview.mp4")
def media_preview_video(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "preview.mp4")


@router.get("/media/previews/{asset_uid}/preview.jpg")
def media_preview_image(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "preview.jpg")


@router.get("/media/assets/previews/{asset_id}/{filename}")
def media_asset_preview(asset_id: int, filename: str, _: AdminUser):
    if filename not in PREVIEW_FILENAMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found")
    return serve_media_path(f"assets/previews/{asset_id}/{filename}")


@router.get("/media/{path:path}")
def media_any_preview(path: str, _: AdminUser):
    return serve_media_path(path)


@router.get("/sources")
def sources_index(request: Request, _: AdminUser, session: DbSession):
    sources = session.scalars(select(Source).order_by(Source.label)).all()
    return templates.TemplateResponse(
        request,
        "sources/index.html",
        {"page_title": "Sources", "sources": sources},
    )


@router.get("/sources/{source_id:int}")
def sources_detail(request: Request, source_id: int, _: AdminUser, session: DbSession):
    source = get_or_404(session, Source, source_id)
    runs = session.scalars(
        select(SourceSyncRun)
        .where(SourceSyncRun.source_id == source.id)
        .order_by(SourceSyncRun.started_at.desc(), SourceSyncRun.id.desc())
        .limit(10)
    ).all()
    latest_run = runs[0] if runs else None
    return templates.TemplateResponse(
        request,
        "sources/detail.html",
        {
            "page_title": source.label,
            "source": source,
            "runs": runs,
            "latest_run": latest_run,
            "report_limit": 200,
        },
    )


@router.post("/sources/{source_id:int}/sync/scan")
def sources_scan(source_id: int, admin_user: AdminUser, session: DbSession):
    scan_source(session, source_id, apply=False, created_by=admin_user)
    return redirect_to(f"/sources/{source_id}")


@router.post("/sources/{source_id:int}/sync/apply")
def sources_apply(source_id: int, admin_user: AdminUser, session: DbSession):
    scan_source(session, source_id, apply=True, created_by=admin_user)
    return redirect_to(f"/sources/{source_id}")


@router.get("/sources/new")
def sources_new(request: Request, _: AdminUser):
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {"page_title": "New source", "source": None, "provider_values": PROVIDER_VALUES},
    )


@router.post("/sources")
def sources_create(
    _: AdminUser,
    session: DbSession,
    source_id: Annotated[str, Form()],
    provider: Annotated[str, Form()],
    label: Annotated[str, Form()],
    rclone_remote: Annotated[str | None, Form()] = None,
    root_path: Annotated[str | None, Form()] = None,
    folder_url: Annotated[str | None, Form()] = None,
    source_role: Annotated[str | None, Form()] = None,
    derived_rclone_remote: Annotated[str | None, Form()] = None,
    derived_root_path: Annotated[str | None, Form()] = None,
    derived_source_id: Annotated[str | None, Form()] = None,
    sync_enabled: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    session.add(
        Source(
            source_id=source_id.strip(),
            provider=provider,
            label=label.strip(),
            rclone_remote=clean_text(rclone_remote),
            root_path=clean_text(root_path),
            folder_url=clean_text(folder_url),
            source_role=clean_text(source_role),
            derived_rclone_remote=clean_text(derived_rclone_remote),
            derived_root_path=clean_text(derived_root_path),
            derived_source_id=clean_text(derived_source_id),
            sync_enabled=clean_bool(sync_enabled),
            enabled=clean_bool(enabled),
        )
    )
    session.commit()
    return redirect_to("/sources")


@router.get("/sources/{source_id:int}/edit")
def sources_edit(request: Request, source_id: int, _: AdminUser, session: DbSession):
    source = get_or_404(session, Source, source_id)
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {
            "page_title": f"Edit {source.label}",
            "source": source,
            "provider_values": PROVIDER_VALUES,
        },
    )


@router.post("/sources/{source_id:int}/edit")
def sources_update(
    source_id: int,
    _: AdminUser,
    session: DbSession,
    public_source_id: Annotated[str, Form(alias="source_id")],
    provider: Annotated[str, Form()],
    label: Annotated[str, Form()],
    rclone_remote: Annotated[str | None, Form()] = None,
    root_path: Annotated[str | None, Form()] = None,
    folder_url: Annotated[str | None, Form()] = None,
    source_role: Annotated[str | None, Form()] = None,
    derived_rclone_remote: Annotated[str | None, Form()] = None,
    derived_root_path: Annotated[str | None, Form()] = None,
    derived_source_id: Annotated[str | None, Form()] = None,
    sync_enabled: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    source = get_or_404(session, Source, source_id)
    source.source_id = public_source_id.strip()
    source.provider = provider
    source.label = label.strip()
    source.rclone_remote = clean_text(rclone_remote)
    source.root_path = clean_text(root_path)
    source.folder_url = clean_text(folder_url)
    source.source_role = clean_text(source_role)
    source.derived_rclone_remote = clean_text(derived_rclone_remote)
    source.derived_root_path = clean_text(derived_root_path)
    source.derived_source_id = clean_text(derived_source_id)
    source.sync_enabled = clean_bool(sync_enabled)
    source.enabled = clean_bool(enabled)
    session.commit()
    return redirect_to("/sources")


@router.get("/brands")
def brands_index(request: Request, _: AdminUser, session: DbSession):
    brands = session.execute(
        select(
            Brand.name,
            Brand.slug,
            func.count(distinct(Asset.id)).label("asset_count"),
            func.count(distinct(Asset.collection)).label("collection_count"),
        )
        .join(Asset, Asset.brand_id == Brand.id)
        .where(Asset.scope == "brand")
        .group_by(Brand.name, Brand.slug)
        .order_by(Brand.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "brands/index.html",
        {"page_title": "Brands", "brands": brands},
    )


@router.get("/titles")
def titles_index(request: Request, _: AdminUser, session: DbSession):
    titles = session.execute(
        select(
            Asset.title_type,
            Asset.title_slug,
            func.coalesce(Asset.title_name, Asset.title).label("title_name"),
            func.count(Asset.id).label("asset_count"),
        )
        .where(Asset.scope == "title", Asset.title_slug.is_not(None))
        .group_by(Asset.title_type, Asset.title_slug, func.coalesce(Asset.title_name, Asset.title))
        .order_by(Asset.title_type, func.coalesce(Asset.title_name, Asset.title))
    ).all()
    return templates.TemplateResponse(
        request,
        "titles/index.html",
        {"page_title": "Titles", "titles": titles},
    )


@router.get("/brands/new")
def brands_new(request: Request, _: AdminUser):
    return templates.TemplateResponse(
        request,
        "brands/form.html",
        {"page_title": "New brand", "brand": None},
    )


@router.post("/brands")
def brands_create(
    _: AdminUser,
    session: DbSession,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    visual_line: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    session.add(
        Brand(
            slug=slug.strip(),
            name=name.strip(),
            description=clean_text(description),
            visual_line=clean_text(visual_line),
            enabled=clean_bool(enabled),
        )
    )
    session.commit()
    return redirect_to("/brands")


@router.get("/brands/{brand_id}/edit")
def brands_edit(request: Request, brand_id: int, _: AdminUser, session: DbSession):
    brand = get_or_404(session, Brand, brand_id)
    return templates.TemplateResponse(
        request,
        "brands/form.html",
        {"page_title": f"Edit {brand.name}", "brand": brand},
    )


@router.post("/brands/{brand_id}/edit")
def brands_update(
    brand_id: int,
    _: AdminUser,
    session: DbSession,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    visual_line: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    brand = get_or_404(session, Brand, brand_id)
    brand.slug = slug.strip()
    brand.name = name.strip()
    brand.description = clean_text(description)
    brand.visual_line = clean_text(visual_line)
    brand.enabled = clean_bool(enabled)
    session.commit()
    return redirect_to("/brands")


@router.get("/products")
def products_index(request: Request, _: AdminUser, session: DbSession):
    products = session.scalars(
        select(Product).options(selectinload(Product.brand)).order_by(Product.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "products/index.html",
        {"page_title": "Products", "products": products},
    )


@router.get("/products/new")
def products_new(request: Request, _: AdminUser, session: DbSession):
    return templates.TemplateResponse(
        request,
        "products/form.html",
        {
            "page_title": "New product",
            "product": None,
            "brands": session.scalars(select(Brand).order_by(Brand.name)).all(),
        },
    )


@router.post("/products")
def products_create(
    _: AdminUser,
    session: DbSession,
    brand_id: Annotated[int, Form()],
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    session.add(
        Product(
            brand_id=brand_id,
            slug=slug.strip(),
            name=name.strip(),
            description=clean_text(description),
            enabled=clean_bool(enabled),
        )
    )
    session.commit()
    return redirect_to("/products")


@router.get("/products/{product_id}/edit")
def products_edit(request: Request, product_id: int, _: AdminUser, session: DbSession):
    product = get_or_404(session, Product, product_id)
    return templates.TemplateResponse(
        request,
        "products/form.html",
        {
            "page_title": f"Edit {product.name}",
            "product": product,
            "brands": session.scalars(select(Brand).order_by(Brand.name)).all(),
        },
    )


@router.post("/products/{product_id}/edit")
def products_update(
    product_id: int,
    _: AdminUser,
    session: DbSession,
    brand_id: Annotated[int, Form()],
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    product = get_or_404(session, Product, product_id)
    product.brand_id = brand_id
    product.slug = slug.strip()
    product.name = name.strip()
    product.description = clean_text(description)
    product.enabled = clean_bool(enabled)
    session.commit()
    return redirect_to("/products")


@router.get("/niches")
def niches_index(request: Request, _: AdminUser, session: DbSession):
    niches = session.execute(
        select(Niche.name, Niche.slug, func.count(AssetNiche.asset_id).label("asset_count"))
        .join(AssetNiche, AssetNiche.niche_id == Niche.id)
        .group_by(Niche.name, Niche.slug)
        .order_by(Niche.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "niches/index.html",
        {"page_title": "Niches", "niches": niches},
    )


@router.get("/niches/new")
def niches_new(request: Request, _: AdminUser):
    return templates.TemplateResponse(
        request,
        "niches/form.html",
        {"page_title": "New niche", "niche": None},
    )


@router.post("/niches")
def niches_create(
    _: AdminUser,
    session: DbSession,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
):
    session.add(Niche(slug=slug.strip(), name=name.strip(), description=clean_text(description)))
    session.commit()
    return redirect_to("/niches")


@router.get("/niches/{niche_id}/edit")
def niches_edit(request: Request, niche_id: int, _: AdminUser, session: DbSession):
    niche = get_or_404(session, Niche, niche_id)
    return templates.TemplateResponse(
        request,
        "niches/form.html",
        {"page_title": f"Edit {niche.name}", "niche": niche},
    )


@router.post("/niches/{niche_id}/edit")
def niches_update(
    niche_id: int,
    _: AdminUser,
    session: DbSession,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
):
    niche = get_or_404(session, Niche, niche_id)
    niche.slug = slug.strip()
    niche.name = name.strip()
    niche.description = clean_text(description)
    session.commit()
    return redirect_to("/niches")
