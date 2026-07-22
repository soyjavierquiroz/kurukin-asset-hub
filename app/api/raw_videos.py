from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.assets import require_asset_hub_api_key
from app.db import get_db_session
from app.models import RawVideo

router = APIRouter(prefix="/raw-videos", tags=["raw-videos"])


@router.get("")
def list_raw_videos(
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    query = select(RawVideo).order_by(RawVideo.updated_at.desc(), RawVideo.id.desc())
    count_query = select(func.count(RawVideo.id))
    if status:
        query = query.where(RawVideo.status == status)
        count_query = count_query.where(RawVideo.status == status)
    raw_videos = session.scalars(query.offset(offset).limit(limit)).all()
    return {
        "count": session.scalar(count_query) or 0,
        "limit": limit,
        "offset": offset,
        "raw_videos": [serialize_raw_video(raw_video) for raw_video in raw_videos],
    }


@router.get("/status")
def raw_video_status(
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, int]:
    rows = session.execute(select(RawVideo.status, func.count(RawVideo.id)).group_by(RawVideo.status)).all()
    counts = {"NEW": 0, "PROCESSING": 0, "DONE": 0, "FAILED": 0}
    counts.update({status: int(count) for status, count in rows})
    return counts


@router.get("/{raw_video_id}")
def get_raw_video(
    raw_video_id: int,
    _: Annotated[None, Depends(require_asset_hub_api_key)],
    session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, Any]:
    raw_video = session.get(RawVideo, raw_video_id)
    if raw_video is None:
        raise HTTPException(status_code=404, detail="Raw video not found")
    return serialize_raw_video(raw_video)


def serialize_raw_video(raw_video: RawVideo) -> dict[str, Any]:
    return {
        "id": raw_video.id,
        "remote_path": raw_video.remote_path,
        "size_bytes": raw_video.size_bytes,
        "duration_seconds": raw_video.duration_seconds,
        "provider": raw_video.provider,
        "status": raw_video.status,
        "segments_generated": raw_video.segments_generated,
        "processed_at": raw_video.processed_at.isoformat() if raw_video.processed_at else None,
        "last_seen_at": raw_video.last_seen_at.isoformat(),
        "last_error": raw_video.last_error,
        "created_at": raw_video.created_at.isoformat(),
        "updated_at": raw_video.updated_at.isoformat(),
    }
