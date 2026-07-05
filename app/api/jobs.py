from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.assets import require_asset_hub_api_key
from app.db import get_db_session
from app.schemas.job_asset_bundle import CreateJobAssetBundleRequest, JobAssetBundleResponse
from app.schemas.job_bundle_materialization import (
    BundleMaterializationResponse,
    MaterializeBundleRequest,
)
from app.services.job_asset_bundles import (
    JobAssetBundleValidationError,
    create_job_asset_bundle,
    get_job_asset_bundle_by_uid,
    get_latest_job_asset_bundle_by_job_id,
    serialize_job_asset_bundle_response,
)
from app.services.job_bundle_materialization import (
    JobBundleMaterializationConfigError,
    JobBundleMaterializationNotFoundError,
    JobBundleMaterializationValidationError,
    get_bundle_materialization,
    get_renderer_manifest,
    materialize_job_asset_bundle,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("/asset-bundles", response_model=JobAssetBundleResponse)
def api_create_job_asset_bundle(
    request: CreateJobAssetBundleRequest,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobAssetBundleResponse:
    try:
        bundle = create_job_asset_bundle(session, request)
        session.commit()
    except JobAssetBundleValidationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception:
        session.rollback()
        raise
    return serialize_job_asset_bundle_response(bundle)


@router.get("/asset-bundles/{bundle_uid}", response_model=JobAssetBundleResponse)
def api_get_job_asset_bundle(
    bundle_uid: str,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobAssetBundleResponse:
    bundle = get_job_asset_bundle_by_uid(session, bundle_uid)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bundle not found")
    return serialize_job_asset_bundle_response(bundle)


@router.get("/{job_id}/asset-bundle", response_model=JobAssetBundleResponse)
def api_get_latest_job_asset_bundle(
    job_id: str,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobAssetBundleResponse:
    bundle = get_latest_job_asset_bundle_by_job_id(session, job_id)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bundle not found")
    return serialize_job_asset_bundle_response(bundle)


@router.get("/asset-bundles/{bundle_uid}/manifest")
def api_get_job_asset_bundle_manifest(
    bundle_uid: str,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, object]:
    bundle = get_job_asset_bundle_by_uid(session, bundle_uid)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bundle not found")
    return bundle.manifest_json


@router.post(
    "/asset-bundles/{bundle_uid}/materialize",
    response_model=BundleMaterializationResponse,
)
def api_materialize_job_asset_bundle(
    bundle_uid: str,
    request: MaterializeBundleRequest,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> BundleMaterializationResponse:
    try:
        result = materialize_job_asset_bundle(session, bundle_uid, force=request.force)
        session.commit()
    except JobBundleMaterializationNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except JobBundleMaterializationConfigError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except JobBundleMaterializationValidationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception:
        session.rollback()
        raise
    return BundleMaterializationResponse(**result)


@router.get(
    "/asset-bundles/{bundle_uid}/materialization",
    response_model=BundleMaterializationResponse,
)
def api_get_job_asset_bundle_materialization(
    bundle_uid: str,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> BundleMaterializationResponse:
    try:
        result = get_bundle_materialization(session, bundle_uid)
    except JobBundleMaterializationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return BundleMaterializationResponse(**result)


@router.get("/asset-bundles/{bundle_uid}/renderer-manifest")
def api_get_job_asset_bundle_renderer_manifest(
    bundle_uid: str,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, object]:
    try:
        manifest = get_renderer_manifest(session, bundle_uid)
        session.commit()
    except JobBundleMaterializationNotFoundError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception:
        session.rollback()
        raise
    return manifest
