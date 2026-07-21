from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Asset, AssetAllowedBrand, Brand, Product
from app.schemas.asset_selection import (
    AssetSelectionRequest,
    AssetSelectionResponse,
    SelectedAsset,
)
from app.services.asset_policy import is_asset_eligible_for_search, resolve_asset_search_policy
from app.services.asset_preview import preview_public_url
from app.services.asset_search import (
    build_asset_search_blob,
    normalize_search_text,
    score_asset_for_query,
    tokenize_query,
)

OVERLAY_HINTS = ("habla", "talking", "subtitulo", "subtitulos", "overlay", "texto", "caption")
LOOP_HINTS = ("background", "fondo", "loop", "loopable", "ambiente", "ambiental")


@dataclass(frozen=True)
class ScoredSelectionCandidate:
    asset: Asset
    score: float
    match_reasons: list[str]


def select_assets(session: Session, request: AssetSelectionRequest) -> AssetSelectionResponse:
    query_text = build_selection_query_text(request)
    brand = session.scalar(
        select(Brand)
        .where(Brand.slug == request.brand_slug)
        .options(selectinload(Brand.asset_policy))
    )
    if brand is None:
        return empty_selection_response(request, query_text)

    product: Product | None = None
    if request.product_slug:
        product = session.scalar(
            select(Product)
            .where(Product.brand_id == brand.id, Product.slug == request.product_slug)
            .options(selectinload(Product.asset_policy))
        )
        if product is None:
            return empty_selection_response(request, query_text)

    candidates = filter_selection_candidates(
        session=session,
        request=request,
        brand=brand,
        product=product,
    )
    scored_candidates: list[ScoredSelectionCandidate] = []
    for asset in candidates:
        scored = score_selection_candidate(asset=asset, request=request, query_text=query_text)
        if request.min_score is not None and scored.score < request.min_score:
            continue
        if tokenize_query(query_text) and scored.score <= 0:
            continue
        scored_candidates.append(scored)

    scored_candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.asset.quality_score or 0,
            candidate.asset.ai_enrichment_confidence or 0,
            -candidate.asset.usage_count,
            candidate.asset.id,
        ),
        reverse=True,
    )
    selected = diversify_selected_assets(
        scored_candidates,
        count=request.count,
        max_per_similarity_group=request.max_per_similarity_group,
    )
    return AssetSelectionResponse(
        count=len(selected),
        requested_count=request.count,
        brand_slug=request.brand_slug,
        product_slug=request.product_slug,
        query_used=query_text,
        assets=[serialize_selected_asset(candidate) for candidate in selected],
    )


def build_selection_query_text(request: AssetSelectionRequest) -> str:
    parts: list[str] = []
    parts.extend(
        value
        for value in (
            request.query,
            request.script_scene,
            request.scene_role,
            request.niche_slug,
        )
        if value
    )
    parts.extend(request.preferred_keywords)
    return " ".join(part.strip() for part in parts if part and part.strip())


def filter_selection_candidates(
    session: Session,
    request: AssetSelectionRequest,
    brand: Brand,
    product: Product | None,
) -> list[Asset]:
    filters = [
        eligible_selection_base_filter(),
        or_(
            Asset.brand_id == brand.id,
            Asset.usage_scope == "global",
            allowed_brand_exists(brand),
        ),
    ]
    if request.asset_type and request.asset_type != "any":
        filters.append(Asset.type == request.asset_type)
    if request.orientation:
        filters.append(Asset.orientation == request.orientation)
    if request.require_preview_ready:
        filters.append(Asset.preview_status == "ready")
    if request.require_ai_ready:
        filters.append(Asset.ai_enrichment_status == "ready")
        filters.append(Asset.needs_human_review.is_(False))
    if not request.allow_needs_review:
        filters.append(Asset.needs_human_review.is_(False))
    if request.exclude_asset_ids:
        filters.append(Asset.id.not_in(request.exclude_asset_ids))
    if request.exclude_asset_uids:
        filters.append(Asset.asset_uid.not_in(request.exclude_asset_uids))

    candidate_limit = max(request.count * 30, 300)
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
            func.coalesce(Asset.quality_score, 0).desc(),
            func.coalesce(Asset.ai_enrichment_confidence, 0).desc(),
            Asset.usage_count.asc(),
            Asset.id.desc(),
        )
        .limit(candidate_limit)
    )
    policy = resolve_asset_search_policy(brand=brand, product=product)
    return [
        asset
        for asset in session.scalars(query).all()
        if is_asset_eligible_for_search(
            asset=asset,
            brand=brand,
            product=product,
            include_global_assets=True,
            include_stock_assets=True,
            policy=policy,
        )
    ]


def score_selection_candidate(
    asset: Asset,
    request: AssetSelectionRequest,
    query_text: str,
) -> ScoredSelectionCandidate:
    tokens = tokenize_query(query_text)
    score = score_asset_for_query(asset, tokens)
    reasons = ["Marca/producto compatible"]
    blob = build_asset_search_blob(asset)

    for keyword in request.preferred_keywords:
        normalized = normalize_search_text(keyword)
        if normalized and normalized in blob:
            score += 4.0
            reasons.append(f"Coincide con keyword: {keyword}")

    for keyword in request.negative_keywords:
        normalized = normalize_search_text(keyword)
        if normalized and normalized in blob:
            score -= 8.0
            reasons.append(f"Penalizado por keyword negativa: {keyword}")

    for token in tokens:
        if token and token in normalize_search_text(asset.best_for):
            score += 2.0
            reasons.append(f"Coincide con best_for: {token}")
            break

    if any(token and token in normalize_search_text(asset.avoid_for) for token in tokens):
        score -= 6.0
        reasons.append("Penalizado por avoid_for")

    if request.scene_role:
        normalized_role = normalize_search_text(request.scene_role)
        if normalized_role and normalized_role == normalize_search_text(asset.best_scene_role):
            score += 5.0
            reasons.append(f"Coincide con scene_role: {asset.best_scene_role}")
        elif normalized_role and normalized_role in normalize_search_text(asset.best_for):
            score += 2.0
            reasons.append(f"best_for sugiere scene_role: {request.scene_role}")

    if request.orientation and asset.orientation == request.orientation:
        score += 3.0
        reasons.append(f"Coincide con orientación {request.orientation}")

    if asset.preview_status == "ready":
        score += 1.5
        reasons.append("Preview listo")
    elif asset.preview_status == "failed":
        score -= 2.0
        reasons.append("Penalizado por preview fallido")

    if asset.ai_enrichment_status == "ready":
        confidence = asset.ai_enrichment_confidence
        score += 1.5 + (confidence or 0.0)
        if confidence is not None:
            reasons.append(f"IA lista con confianza {confidence:.2f}")
        else:
            reasons.append("IA lista")
    elif asset.ai_enrichment_status in {"failed", "needs_review"}:
        score -= 2.0
        reasons.append(f"Penalizado por IA {asset.ai_enrichment_status}")

    if asset.needs_human_review:
        score -= 3.0
        reasons.append("Penalizado por needs_review")

    if asset.ai_enrichment_confidence is not None and asset.ai_enrichment_confidence < 0.5:
        score -= 1.5
        reasons.append(f"Penalizado por baja confianza IA {asset.ai_enrichment_confidence:.2f}")

    normalized_context = normalize_search_text(
        " ".join(
            value
            for value in (request.query, request.script_scene, request.scene_role)
            if value
        )
    )
    if any(hint in normalized_context for hint in OVERLAY_HINTS) and asset.safe_for_subtitles:
        score += 2.0
        reasons.append("Seguro para subtítulos/overlay")
    if any(hint in normalized_context for hint in LOOP_HINTS) and asset.loopable:
        score += 2.0
        reasons.append("Loopable para fondo")

    return ScoredSelectionCandidate(asset=asset, score=round(score, 4), match_reasons=reasons)


def diversify_selected_assets(
    candidates: list[ScoredSelectionCandidate],
    count: int,
    max_per_similarity_group: int = 1,
) -> list[ScoredSelectionCandidate]:
    selected: list[ScoredSelectionCandidate] = []
    seen_ids: set[int] = set()
    seen_uids: set[str] = set()
    group_counts: dict[str, int] = {}
    for candidate in candidates:
        asset = candidate.asset
        if asset.id in seen_ids or asset.asset_uid in seen_uids:
            continue
        if asset.similarity_group:
            current_count = group_counts.get(asset.similarity_group, 0)
            if current_count >= max_per_similarity_group:
                continue
            group_counts[asset.similarity_group] = current_count + 1
        selected.append(candidate)
        seen_ids.add(asset.id)
        seen_uids.add(asset.asset_uid)
        if len(selected) >= count:
            break
    return selected


def eligible_selection_base_filter():
    return and_(
        Asset.status == "active",
        or_(
            Asset.source_status.is_(None),
            Asset.source_status.not_in(("missing", "inaccessible", "deleted")),
        ),
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


def serialize_selected_asset(candidate: ScoredSelectionCandidate) -> SelectedAsset:
    asset = candidate.asset
    return SelectedAsset(
        id=asset.id,
        asset_uid=asset.asset_uid,
        filename=asset.filename,
        title=asset.title,
        type=asset.type,
        mime_type=asset.mime_type,
        brand_slug=asset.brand.slug if asset.brand else None,
        product_slug=asset.product.slug if asset.product else None,
        source_id=asset.source.source_id if asset.source else None,
        rclone_remote=asset.rclone_remote or (asset.source.rclone_remote if asset.source else None),
        remote_path=asset.remote_path,
        usage_scope=asset.usage_scope,
        rights_status=asset.rights_status,
        preview_status=asset.preview_status,
        ai_enrichment_status=asset.ai_enrichment_status,
        needs_human_review=asset.needs_human_review,
        review_reason=asset.review_reason,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        orientation=asset.orientation,
        fps=asset.fps,
        codec=asset.codec,
        has_audio=asset.has_audio,
        shot_type=asset.shot_type,
        camera_motion=asset.camera_motion,
        subject_position=asset.subject_position,
        visual_energy=asset.visual_energy,
        pacing=asset.pacing,
        best_scene_role=asset.best_scene_role,
        safe_for_subtitles=asset.safe_for_subtitles,
        safe_for_text_overlay=asset.safe_for_text_overlay,
        overlay_safe_area=asset.overlay_safe_area,
        flip_horizontal_allowed=asset.flip_horizontal_allowed,
        crop_allowed=asset.crop_allowed,
        zoom_allowed=asset.zoom_allowed,
        speed_change_allowed=asset.speed_change_allowed,
        reverse_allowed=asset.reverse_allowed,
        color_grade_allowed=asset.color_grade_allowed,
        loopable=asset.loopable,
        best_for=asset.best_for,
        avoid_for=asset.avoid_for,
        search_text=asset.search_text,
        score=candidate.score,
        match_reasons=candidate.match_reasons,
        thumbnail_url=media_preview_url(asset.thumbnail_path),
        preview_url=media_preview_url(asset.preview_path),
    )


def media_preview_url(path: str | None) -> str | None:
    return preview_public_url(path)


def empty_selection_response(
    request: AssetSelectionRequest,
    query_text: str,
) -> AssetSelectionResponse:
    return AssetSelectionResponse(
        count=0,
        requested_count=request.count,
        brand_slug=request.brand_slug,
        product_slug=request.product_slug,
        query_used=query_text,
        assets=[],
    )
