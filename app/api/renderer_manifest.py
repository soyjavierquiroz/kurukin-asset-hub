from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.assets import require_asset_hub_api_key
from app.schemas.renderer_manifest import RendererManifest

router = APIRouter(prefix="/api/renderer-manifest", tags=["renderer-manifest"])


@router.get("/schema")
def api_get_renderer_manifest_schema(
    _: Annotated[None, Depends(require_asset_hub_api_key)],
) -> dict[str, Any]:
    return RendererManifest.model_json_schema()
