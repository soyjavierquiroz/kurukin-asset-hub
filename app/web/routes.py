from collections import defaultdict
import math
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db_session
from app.models import (
    Asset,
    AssetAllowedBrand,
    AssetKeyword,
    Brand,
    Niche,
    Product,
    Source,
    SourceSyncRun,
)
from app.models.asset import (
    AI_ENRICHMENT_STATUS_VALUES,
    ASSET_STATUS_VALUES,
    ASSET_TYPE_VALUES,
    ORIENTATION_VALUES,
    USAGE_SCOPE_VALUES,
)
from app.models.common import PROVIDER_VALUES
from app.services.asset_preview import (
    PREVIEW_FILENAMES,
    generate_asset_preview,
    preview_output_dir,
    safe_asset_uid,
)
from app.services.ai_asset_enrichment import enrich_asset_with_ai
from app.services.source_sync import scan_source

router = APIRouter(tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()


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
    product_id: int | None = None,
    niche_id: int | None = None,
    type: str | None = None,
    orientation: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    usage_scope: str | None = None,
    auto_select_enabled: bool | None = None,
    ai_status: str | None = None,
    needs_review: bool | None = None,
    page: int = 1,
):
    page = max(page, 1)
    per_page = 25
    filters = []

    if clean_text(q):
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Asset.asset_uid.ilike(like),
                Asset.filename.ilike(like),
                Asset.remote_path.ilike(like),
                Asset.title.ilike(like),
                Asset.description.ilike(like),
            )
        )
    if brand_id:
        filters.append(Asset.brand_id == brand_id)
    if product_id:
        filters.append(Asset.product_id == product_id)
    if type:
        filters.append(Asset.type == type)
    if orientation:
        filters.append(Asset.orientation == orientation)
    if status:
        filters.append(Asset.status == status)
    if clean_text(keyword):
        like = f"%{keyword.strip()}%"
        filters.append(Asset.keywords.any(AssetKeyword.keyword.ilike(like)))
    if usage_scope:
        filters.append(Asset.usage_scope == usage_scope)
    if auto_select_enabled is not None:
        filters.append(Asset.auto_select_enabled.is_(auto_select_enabled))
    if ai_status:
        filters.append(Asset.ai_enrichment_status == ai_status)
    if needs_review is not None:
        filters.append(Asset.needs_human_review.is_(needs_review))

    query: Select[tuple[Asset]] = select(Asset).options(
        selectinload(Asset.source),
        selectinload(Asset.brand),
        selectinload(Asset.product),
        selectinload(Asset.niches),
        selectinload(Asset.keywords),
    )
    count_query = select(func.count(func.distinct(Asset.id))).select_from(Asset)

    if niche_id:
        query = query.join(Asset.niches).where(Niche.id == niche_id)
        count_query = count_query.join(Asset.niches).where(Niche.id == niche_id)
    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total = session.scalar(count_query) or 0
    needs_review_count = session.scalar(count_query.where(Asset.needs_human_review.is_(True))) or 0
    assets = session.scalars(
        query.order_by(Asset.created_at.desc(), Asset.id.desc())
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
            "brands": session.scalars(select(Brand).order_by(Brand.name)).all(),
            "products": session.scalars(select(Product).order_by(Product.name)).all(),
            "niches": session.scalars(select(Niche).order_by(Niche.name)).all(),
            "filters": {
                "q": q or "",
                "brand_id": brand_id,
                "product_id": product_id,
                "niche_id": niche_id,
                "type": type or "",
                "orientation": orientation or "",
                "status": status or "",
                "keyword": keyword or "",
                "usage_scope": usage_scope or "",
                "auto_select_enabled": auto_select_enabled,
                "ai_status": ai_status or "",
                "needs_review": needs_review,
            },
            "type_values": ASSET_TYPE_VALUES,
            "orientation_values": ORIENTATION_VALUES,
            "status_values": ASSET_STATUS_VALUES,
            "usage_scope_values": USAGE_SCOPE_VALUES,
            "ai_status_values": AI_ENRICHMENT_STATUS_VALUES,
            "pagination_query": build_query_string(
                {
                    "q": q,
                    "brand_id": brand_id,
                    "product_id": product_id,
                    "niche_id": niche_id,
                    "type": type,
                    "orientation": orientation,
                    "status": status,
                    "keyword": keyword,
                    "usage_scope": usage_scope,
                    "auto_select_enabled": auto_select_enabled,
                    "ai_status": ai_status,
                    "needs_review": needs_review,
                }
            ),
            "page": page,
            "pages": pages,
            "total": total,
            "needs_review_count": needs_review_count,
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
            selectinload(Asset.allowed_brands).selectinload(
                AssetAllowedBrand.brand,
            ),
            selectinload(Asset.usages),
            selectinload(Asset.collections),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
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
            "ai_keywords_by_category": dict(ai_keywords_by_category),
        },
    )


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


@router.get("/media/previews/{asset_uid}/thumbnail.jpg")
def media_preview_thumbnail(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "thumbnail.jpg")


@router.get("/media/previews/{asset_uid}/preview.mp4")
def media_preview_video(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "preview.mp4")


@router.get("/media/previews/{asset_uid}/preview.jpg")
def media_preview_image(asset_uid: str, _: AdminUser):
    return serve_preview_file(asset_uid, "preview.jpg")


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
    source.sync_enabled = clean_bool(sync_enabled)
    source.enabled = clean_bool(enabled)
    session.commit()
    return redirect_to("/sources")


@router.get("/brands")
def brands_index(request: Request, _: AdminUser, session: DbSession):
    brands = session.scalars(select(Brand).order_by(Brand.name)).all()
    return templates.TemplateResponse(
        request,
        "brands/index.html",
        {"page_title": "Brands", "brands": brands},
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
    niches = session.scalars(select(Niche).order_by(Niche.name)).all()
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
