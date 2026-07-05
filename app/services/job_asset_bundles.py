from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Asset, Brand, JobAssetBundle, JobAssetBundleItem, Niche, Product
from app.schemas.asset_selection import AssetSelectionRequest, SelectedAsset
from app.schemas.job_asset_bundle import (
    CreateJobAssetBundleRequest,
    JobAssetBundleResponse,
    SceneAssetRequest,
    SelectedBundleAsset,
)
from app.services.asset_selection import select_assets


class JobAssetBundleValidationError(ValueError):
    pass


def create_job_asset_bundle(
    session: Session,
    request: CreateJobAssetBundleRequest,
) -> JobAssetBundle:
    brand = session.scalar(select(Brand).where(Brand.slug == request.brand_slug))
    if brand is None:
        raise JobAssetBundleValidationError(f"Brand not found: {request.brand_slug}")

    product: Product | None = None
    if request.product_slug:
        product = session.scalar(
            select(Product).where(
                Product.brand_id == brand.id,
                Product.slug == request.product_slug,
            )
        )
        if product is None:
            raise JobAssetBundleValidationError(f"Product not found: {request.product_slug}")

    niche: Niche | None = None
    if request.niche_slug:
        niche = session.scalar(select(Niche).where(Niche.slug == request.niche_slug))
        if niche is None:
            raise JobAssetBundleValidationError(f"Niche not found: {request.niche_slug}")

    if not request.force:
        existing = get_latest_ready_job_asset_bundle_by_job_id(session, request.job_id)
        if existing is not None:
            return existing

    if request.force:
        supersede_existing_job_bundles(session, request.job_id)

    bundle_uid = f"jab_{uuid4().hex}"
    selected_asset_ids: set[int] = set(request.global_exclude_asset_ids)
    selected_asset_uids: set[str] = set(request.global_exclude_asset_uids)
    scene_manifests: list[dict[str, object]] = []
    item_payloads: list[dict[str, object]] = []

    for scene in request.scenes:
        selection_request = build_scene_selection_request(
            request=request,
            scene=scene,
            exclude_asset_ids=selected_asset_ids,
            exclude_asset_uids=selected_asset_uids,
        )
        selection = select_assets(session, selection_request)
        asset_rows = load_assets_by_id(session, [asset.id for asset in selection.assets])
        scene_assets: list[dict[str, object]] = []

        for rank, selected_asset in enumerate(selection.assets, start=1):
            selected_asset_ids.add(selected_asset.id)
            selected_asset_uids.add(selected_asset.asset_uid)
            asset_row = asset_rows.get(selected_asset.id)
            response_asset = serialize_selected_asset_for_bundle(
                scene=scene,
                rank=rank,
                selected_asset=selected_asset,
            )
            item_payloads.append(
                {
                    "scene_id": scene.scene_id,
                    "scene_index": scene.scene_index,
                    "asset_id": selected_asset.id,
                    "asset_uid": selected_asset.asset_uid,
                    "score": selected_asset.score,
                    "rank": rank,
                    "match_reasons": selected_asset.match_reasons,
                    "selection_json": response_asset.model_dump(mode="json"),
                }
            )
            scene_assets.append(
                serialize_manifest_asset(
                    selected_asset=selected_asset,
                    asset=asset_row,
                )
            )

        scene_manifests.append(
            {
                "scene_id": scene.scene_id,
                "scene_index": scene.scene_index,
                "script_scene": scene.script_scene,
                "requested_count": scene.count,
                "selected_count": len(scene_assets),
                "assets": scene_assets,
            }
        )

    total_assets = len(item_payloads)
    status = resolve_bundle_status(scene_manifests)
    manifest = build_bundle_manifest(
        bundle_uid=bundle_uid,
        request=request,
        scenes=scene_manifests,
    )

    bundle = JobAssetBundle(
        bundle_uid=bundle_uid,
        job_id=request.job_id,
        brand=brand,
        product=product,
        niche=niche,
        status=status,
        request_json=request.model_dump(mode="json"),
        manifest_json=manifest,
        total_scenes=len(request.scenes),
        total_assets=total_assets,
        created_by=request.created_by,
        error="No assets selected for any scene" if status == "failed" else None,
    )
    bundle.items = [JobAssetBundleItem(**payload) for payload in item_payloads]
    session.add(bundle)
    session.flush()
    return bundle


def get_job_asset_bundle_by_uid(session: Session, bundle_uid: str) -> JobAssetBundle | None:
    return session.scalar(bundle_query().where(JobAssetBundle.bundle_uid == bundle_uid))


def get_latest_job_asset_bundle_by_job_id(session: Session, job_id: str) -> JobAssetBundle | None:
    return session.scalar(
        bundle_query()
        .where(JobAssetBundle.job_id == job_id)
        .order_by(JobAssetBundle.created_at.desc(), JobAssetBundle.id.desc())
    )


def get_latest_ready_job_asset_bundle_by_job_id(
    session: Session,
    job_id: str,
) -> JobAssetBundle | None:
    return session.scalar(
        bundle_query()
        .where(JobAssetBundle.job_id == job_id, JobAssetBundle.status == "ready")
        .order_by(JobAssetBundle.created_at.desc(), JobAssetBundle.id.desc())
    )


def build_bundle_manifest(
    bundle_uid: str,
    request: CreateJobAssetBundleRequest,
    scenes: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "bundle_uid": bundle_uid,
        "job_id": request.job_id,
        "brand_slug": request.brand_slug,
        "product_slug": request.product_slug,
        "generated_at": datetime.now(UTC).isoformat(),
        "scenes": scenes,
    }


def serialize_selected_asset_for_bundle(
    scene: SceneAssetRequest,
    rank: int,
    selected_asset: SelectedAsset,
) -> SelectedBundleAsset:
    return SelectedBundleAsset(
        scene_id=scene.scene_id,
        scene_index=scene.scene_index,
        rank=rank,
        id=selected_asset.id,
        asset_uid=selected_asset.asset_uid,
        filename=selected_asset.filename,
        type=selected_asset.type,
        brand_slug=selected_asset.brand_slug,
        product_slug=selected_asset.product_slug,
        source_id=selected_asset.source_id,
        rclone_remote=selected_asset.rclone_remote,
        remote_path=selected_asset.remote_path,
        usage_scope=selected_asset.usage_scope,
        rights_status=selected_asset.rights_status,
        preview_status=selected_asset.preview_status,
        ai_enrichment_status=selected_asset.ai_enrichment_status,
        needs_human_review=selected_asset.needs_human_review,
        duration_seconds=selected_asset.duration_seconds,
        width=selected_asset.width,
        height=selected_asset.height,
        orientation=selected_asset.orientation,
        thumbnail_url=selected_asset.thumbnail_url,
        preview_url=selected_asset.preview_url,
        score=selected_asset.score,
        match_reasons=selected_asset.match_reasons,
    )


def serialize_job_asset_bundle_response(bundle: JobAssetBundle) -> JobAssetBundleResponse:
    assets = [
        SelectedBundleAsset(**item.selection_json)
        for item in sorted(
            bundle.items,
            key=lambda bundle_item: (
                bundle_item.scene_index if bundle_item.scene_index is not None else 999999,
                bundle_item.scene_id,
                bundle_item.rank if bundle_item.rank is not None else 999999,
                bundle_item.id,
            ),
        )
    ]
    brand_slug = (
        bundle.brand.slug
        if bundle.brand
        else str(bundle.manifest_json.get("brand_slug", ""))
    )
    return JobAssetBundleResponse(
        bundle_uid=bundle.bundle_uid,
        job_id=bundle.job_id,
        status=bundle.status,
        brand_slug=brand_slug,
        product_slug=bundle.product.slug if bundle.product else None,
        total_scenes=bundle.total_scenes,
        total_assets=bundle.total_assets,
        assets=assets,
        manifest=bundle.manifest_json,
        created_at=bundle.created_at,
        updated_at=bundle.updated_at,
        error=bundle.error,
    )


def build_scene_selection_request(
    request: CreateJobAssetBundleRequest,
    scene: SceneAssetRequest,
    exclude_asset_ids: set[int],
    exclude_asset_uids: set[str],
) -> AssetSelectionRequest:
    return AssetSelectionRequest(
        brand_slug=request.brand_slug,
        product_slug=request.product_slug,
        niche_slug=request.niche_slug,
        query=scene.query,
        script_scene=scene.script_scene,
        scene_role=scene.scene_role,
        asset_type=scene.asset_type,
        orientation=scene.orientation,
        count=scene.count,
        exclude_asset_ids=sorted(exclude_asset_ids.union(scene.exclude_asset_ids)),
        exclude_asset_uids=sorted(exclude_asset_uids.union(scene.exclude_asset_uids)),
        require_preview_ready=scene.require_preview_ready,
        require_ai_ready=scene.require_ai_ready,
        allow_needs_review=scene.allow_needs_review,
        preferred_keywords=scene.preferred_keywords,
        negative_keywords=scene.negative_keywords,
        max_per_similarity_group=scene.max_per_similarity_group,
    )


def resolve_bundle_status(scenes: list[dict[str, object]]) -> str:
    selected_counts = [
        selected_count
        for scene in scenes
        if isinstance(selected_count := scene.get("selected_count"), int)
    ]
    total_assets = sum(selected_counts)
    if total_assets == 0:
        return "failed"
    if any(selected_count == 0 for selected_count in selected_counts):
        return "partial"
    return "ready"


def supersede_existing_job_bundles(session: Session, job_id: str) -> None:
    bundles = session.scalars(
        select(JobAssetBundle).where(
            JobAssetBundle.job_id == job_id,
            JobAssetBundle.status != "superseded",
        )
    ).all()
    for bundle in bundles:
        bundle.status = "superseded"


def load_assets_by_id(session: Session, asset_ids: list[int]) -> dict[int, Asset]:
    if not asset_ids:
        return {}
    assets = session.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    return {asset.id: asset for asset in assets}


def serialize_manifest_asset(
    selected_asset: SelectedAsset,
    asset: Asset | None,
) -> dict[str, object]:
    return {
        "asset_id": selected_asset.id,
        "asset_uid": selected_asset.asset_uid,
        "filename": selected_asset.filename,
        "rclone_remote": selected_asset.rclone_remote,
        "remote_path": selected_asset.remote_path,
        "type": selected_asset.type,
        "duration_seconds": selected_asset.duration_seconds,
        "orientation": selected_asset.orientation,
        "score": selected_asset.score,
        "match_reasons": selected_asset.match_reasons,
        "needs_human_review": selected_asset.needs_human_review,
        "preview_path": asset.preview_path if asset else None,
        "thumbnail_path": asset.thumbnail_path if asset else None,
    }


def bundle_query():
    return select(JobAssetBundle).options(
        selectinload(JobAssetBundle.brand),
        selectinload(JobAssetBundle.product),
        selectinload(JobAssetBundle.niche),
        selectinload(JobAssetBundle.items),
    )
