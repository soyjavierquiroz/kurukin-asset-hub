from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Asset
from app.services.rclone_service import RcloneService, sanitize_rclone_message

TEMP_ROOT = Path("/tmp/kurukin-asset-hub-probe")
SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PREVIEW_FILENAMES = frozenset({"thumbnail.jpg", "preview.mp4", "preview.jpg"})
VIDEO_PREVIEW_MAX_SECONDS = 30


class AssetPreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedProbeMetadata:
    duration_seconds: float | None
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    has_audio: bool


def generate_asset_preview(session: Session, asset_id: int, force: bool = False) -> Asset:
    asset = session.scalar(
        select(Asset).where(Asset.id == asset_id).options(selectinload(Asset.source))
    )
    if asset is None:
        raise AssetPreviewError(f"Asset not found: {asset_id}")

    if asset.preview_status == "ready" and not force:
        return asset

    try:
        _validate_asset(asset)
    except AssetPreviewError as exc:
        message = sanitize_error_message(str(exc))
        asset.preview_status = "failed"
        asset.technical_metadata_status = "failed"
        asset.preview_error = message
        asset.probe_error = message
        session.commit()
        return asset
    asset.preview_status = "processing"
    asset.technical_metadata_status = "processing"
    asset.preview_error = None
    asset.probe_error = None
    session.commit()

    safe_uid = safe_asset_uid(asset.asset_uid)
    temp_dir = TEMP_ROOT / safe_uid
    download_path = temp_dir / safe_filename(asset.filename)
    technical_ready = False

    try:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        RcloneService().copyto(asset.rclone_remote or "", asset.remote_path, str(download_path))

        probe_data = run_ffprobe(download_path)
        metadata = parse_ffprobe_metadata(probe_data)
        _apply_metadata(asset, metadata)
        asset.technical_metadata_status = "ready"
        technical_ready = True
        session.commit()

        output_dir = preview_output_dir(asset.asset_uid)
        output_dir.mkdir(parents=True, exist_ok=True)

        thumbnail_path: str | None = None
        preview_path: str | None = None
        if asset.type == "video":
            thumbnail_file = output_dir / "thumbnail.jpg"
            preview_file = output_dir / "preview.mp4"
            generate_video_thumbnail(download_path, thumbnail_file)
            generate_video_preview(download_path, preview_file)
            thumbnail_path = relative_preview_path(asset.asset_uid, "thumbnail.jpg")
            preview_path = relative_preview_path(asset.asset_uid, "preview.mp4")
        elif asset.type == "image":
            thumbnail_file = output_dir / "thumbnail.jpg"
            preview_file = output_dir / "preview.jpg"
            generate_image_preview(download_path, thumbnail_file, preview_file)
            thumbnail_path = relative_preview_path(asset.asset_uid, "thumbnail.jpg")
            preview_path = relative_preview_path(asset.asset_uid, "preview.jpg")
        elif asset.type == "audio":
            asset.preview_status = "skipped"
        else:
            asset.preview_status = "skipped"

        if thumbnail_path or preview_path:
            asset.thumbnail_path = thumbnail_path
            asset.preview_path = preview_path
            asset.preview_status = "ready"
        else:
            asset.thumbnail_path = None
            asset.preview_path = None
        asset.preview_generated_at = datetime.now(UTC)
        session.commit()
        return asset
    except BaseException as exc:
        session.rollback()
        failed_asset = session.get(Asset, asset_id)
        if failed_asset is None:
            raise
        message = sanitize_error_message(str(exc))
        failed_asset.preview_status = "failed"
        failed_asset.preview_error = message
        if technical_ready:
            failed_asset.technical_metadata_status = "ready"
        else:
            failed_asset.technical_metadata_status = "failed"
            failed_asset.probe_error = message
        session.commit()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return failed_asset
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise AssetPreviewError("ffprobe binary was not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssetPreviewError("ffprobe timed out") from exc

    if result.returncode != 0:
        message = sanitize_error_message(result.stderr or result.stdout or "ffprobe failed")
        raise AssetPreviewError(f"ffprobe failed: {message}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssetPreviewError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AssetPreviewError("ffprobe returned an unexpected JSON payload")
    return payload


def parse_ffprobe_metadata(data: dict[str, Any]) -> ParsedProbeMetadata:
    streams = data.get("streams")
    if not isinstance(streams, list):
        streams = []
    format_data = data.get("format")
    if not isinstance(format_data, dict):
        format_data = {}

    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )

    duration = parse_float(format_data.get("duration"))
    if duration is None and isinstance(video_stream, dict):
        duration = parse_float(video_stream.get("duration"))
    if duration is None and isinstance(audio_stream, dict):
        duration = parse_float(audio_stream.get("duration"))

    width = parse_int(video_stream.get("width")) if isinstance(video_stream, dict) else None
    height = parse_int(video_stream.get("height")) if isinstance(video_stream, dict) else None
    fps = None
    codec = None
    if isinstance(video_stream, dict):
        fps = parse_fps(video_stream.get("avg_frame_rate")) or parse_fps(
            video_stream.get("r_frame_rate")
        )
        codec = clean_codec(video_stream.get("codec_name"))
    if codec is None and isinstance(audio_stream, dict):
        codec = clean_codec(audio_stream.get("codec_name"))

    return ParsedProbeMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        fps=fps,
        codec=codec,
        has_audio=audio_stream is not None,
    )


def infer_orientation(width: int | None, height: int | None) -> str:
    if not width or not height or width <= 0 or height <= 0:
        return "unknown"
    ratio = width / height
    if 0.9 <= ratio <= 1.1:
        return "square"
    if height > width and 0.45 <= ratio <= 0.7:
        return "9:16"
    if width > height and 1.5 <= ratio <= 1.9:
        return "16:9"
    return "unknown"


def generate_video_thumbnail(input_path: Path, output_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "thumbnail,scale=w='if(gte(iw,ih),min(720,iw),-2)':h='if(gte(iw,ih),-2,min(720,ih))'",
        "-frames:v",
        "1",
        str(output_path),
    ]
    run_ffmpeg(command, "video thumbnail")


def generate_video_preview(input_path: Path, output_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale=w='if(gte(iw,ih),min(720,iw),-2)':h='if(gte(iw,ih),-2,min(720,ih))'",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-t",
        str(VIDEO_PREVIEW_MAX_SECONDS),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_ffmpeg(command, "video preview", timeout=900)


def generate_image_preview(input_path: Path, thumbnail_path: Path, preview_path: Path) -> None:
    thumbnail_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale=w='if(gte(iw,ih),min(360,iw),-2)':h='if(gte(iw,ih),-2,min(360,ih))'",
        "-frames:v",
        "1",
        str(thumbnail_path),
    ]
    run_ffmpeg(thumbnail_command, "image thumbnail")
    preview_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale=w='if(gte(iw,ih),min(1280,iw),-2)':h='if(gte(iw,ih),-2,min(1280,ih))'",
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(preview_path),
    ]
    run_ffmpeg(preview_command, "image preview")


def run_ffmpeg(command: list[str], operation: str, timeout: int = 300) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AssetPreviewError("ffmpeg binary was not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssetPreviewError(f"ffmpeg {operation} timed out") from exc

    if result.returncode != 0:
        message = sanitize_error_message(result.stderr or result.stdout or f"{operation} failed")
        raise AssetPreviewError(f"ffmpeg {operation} failed: {message}")


def preview_output_dir(asset_uid: str) -> Path:
    return Path(get_settings().preview_storage_dir) / safe_asset_uid(asset_uid)


def relative_preview_path(asset_uid: str, filename: str) -> str:
    if filename not in PREVIEW_FILENAMES:
        raise AssetPreviewError("Unsupported preview filename")
    return f"previews/{safe_asset_uid(asset_uid)}/{filename}"


def safe_asset_uid(asset_uid: str) -> str:
    cleaned = SAFE_PATH_RE.sub("_", asset_uid).strip("._")
    if not cleaned:
        raise AssetPreviewError("Asset UID is not safe for preview storage")
    return cleaned[:160]


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    cleaned = SAFE_PATH_RE.sub("_", name).strip("._")
    return cleaned[:180] or "asset"


def sanitize_error_message(message: str) -> str:
    sanitized = sanitize_rclone_message(message)
    rclone_config = str(Path.home() / ".config" / "rclone" / "rclone.conf")
    sanitized = sanitized.replace(rclone_config, "[rclone_config]")
    sanitized = re.sub(r"/config/rclone/rclone\.conf", "[rclone_config]", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized[:700] or "Preview enrichment failed"


def parse_float(value: object) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: object) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_fps(value: object) -> float | None:
    if not isinstance(value, str) or value in {"", "0/0", "N/A"}:
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        num = parse_float(numerator)
        den = parse_float(denominator)
        if num is None or den in (None, 0):
            return None
        return num / den
    return parse_float(value)


def clean_codec(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:120] or None


def _validate_asset(asset: Asset) -> None:
    if not asset.rclone_remote:
        raise AssetPreviewError("Asset has no rclone remote")
    if not asset.remote_path:
        raise AssetPreviewError("Asset has no remote path")
    if asset.source is None or not asset.source.source_id:
        raise AssetPreviewError("Asset has no source")


def _apply_metadata(asset: Asset, metadata: ParsedProbeMetadata) -> None:
    asset.duration_seconds = metadata.duration_seconds
    asset.width = metadata.width
    asset.height = metadata.height
    asset.fps = metadata.fps
    asset.codec = metadata.codec
    asset.has_audio = metadata.has_audio
    asset.orientation = infer_orientation(metadata.width, metadata.height)
