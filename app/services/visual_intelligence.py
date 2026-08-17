from __future__ import annotations

import json
import math
import random
import shutil
import socket
import subprocess
import time
import urllib.error
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Asset, AssetAIAnalysis
from app.schemas.visual_intelligence import (
    VISUAL_ANALYSIS_TYPE,
    VISUAL_FRAME_COUNT,
    VISUAL_PROFILE_VERSION,
    VisualIntelligenceResult,
)
from app.services.ai_providers.nvidia_provider import (
    NvidiaProviderError,
    extract_json_object,
    post_chat_completion,
)
from app.services.ai_providers.openai_provider import sanitize_provider_error
from app.services.asset_location import AssetLocationError, resolve_asset_rclone_location
from app.services.asset_preview import parse_ffprobe_metadata, run_ffprobe, safe_asset_uid
from app.services.rclone_service import RcloneService

VISUAL_TEMP_ROOT = Path("/tmp/kurukin-asset-hub-visual-intelligence")
VISUAL_TERMINAL_STATUSES = ("ready", "failed")
HIGH_CONFIDENCE_THRESHOLD = 0.75
LOW_CONFIDENCE_THRESHOLD = 0.60
MIN_CACHED_VISUAL_FRAMES = 8
CONTACT_SHEET_FILENAME = "contact-sheet.jpg"
CONTACT_SHEET_MAX_DIMENSION = 1800
VISUAL_V1_NVIDIA_MAX_TOKENS = 2400
VISUAL_NVIDIA_RETRY_DELAYS = (5.0, 15.0, 30.0)
TRANSIENT_NVIDIA_HTTP_STATUSES = {429, 500, 502, 503, 504}
MAX_VISUAL_V1_ZOOM = 1.10
HARD_REJECT_FLAGS = (
    "subject_severely_out_of_frame",
    "subject_badly_clipped",
    "social_media_ui",
    "subscribe_cta",
    "emoji_overlay",
    "nearly_empty",
    "severe_blur",
    "severe_black_frames",
)
QUARANTINE_FLAGS = ("watermark", "heavy_text_overlay")


class VisualIntelligenceOperationalError(RuntimeError):
    pass


class VisualPayloadJSONError(ValueError):
    pass


@dataclass(frozen=True)
class VisualFrameInputs:
    master_path: Path
    frame_paths: list[Path]
    contact_sheet_path: Path
    frame_cache_dir: Path
    temp_dir: Path
    sample_timestamps: list[float | None]


@dataclass(frozen=True)
class VisualBackfillResult:
    dry_run: bool
    profile_version: str
    limit: int | None
    batch_size: int
    selected: int
    processed: int
    skipped: int
    failed: int
    remaining: int
    title_slug: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        if data["title_slug"] is None:
            data.pop("title_slug")
        return data


VisualCaller = Callable[[str, list[Path]], VisualIntelligenceResult]


@dataclass(frozen=True)
class VisualEditorialDecision:
    status: str
    reason_codes: list[str]


@dataclass(frozen=True)
class NormalizedTransformDecision:
    status: str
    allowed: bool
    confidence: float
    blockers: list[str]
    reasons: list[str]


@dataclass(frozen=True)
class NormalizedZoomDecision(NormalizedTransformDecision):
    max_safe_zoom: float


@dataclass(frozen=True)
class NormalizedPanDecision(NormalizedTransformDecision):
    safe_directions: list[str]
    max_offset_x: float | None
    max_offset_y: float | None


@dataclass(frozen=True)
class NormalizedCropDecision(NormalizedTransformDecision):
    safe_rect: list[float] | None


@dataclass(frozen=True)
class NormalizedVisualTransforms:
    flip_horizontal: NormalizedTransformDecision
    zoom: NormalizedZoomDecision
    pan: NormalizedPanDecision
    crop_vertical: NormalizedCropDecision
    crop_horizontal: NormalizedCropDecision
    safe_text_areas: list[str]
    consistency_warnings: list[str]

    @property
    def flip_allowed(self) -> bool:
        return self.flip_horizontal.allowed

    @property
    def flip_risk_reasons(self) -> list[str]:
        return self.flip_horizontal.reasons

    @property
    def zoom_allowed(self) -> bool:
        return self.zoom.allowed

    @property
    def max_safe_zoom(self) -> float:
        return self.zoom.max_safe_zoom

    @property
    def pan_allowed(self) -> bool:
        return self.pan.allowed

    @property
    def pan_safe_directions(self) -> list[str]:
        return self.pan.safe_directions

    @property
    def pan_max_offset_x(self) -> float | None:
        return self.pan.max_offset_x

    @property
    def pan_max_offset_y(self) -> float | None:
        return self.pan.max_offset_y

    @property
    def crop_vertical_allowed(self) -> bool:
        return self.crop_vertical.allowed

    @property
    def crop_horizontal_allowed(self) -> bool:
        return self.crop_horizontal.allowed

    @property
    def crop_allowed(self) -> bool:
        return self.crop_vertical.allowed or self.crop_horizontal.allowed


def analyze_asset_visual_intelligence(
    session: Session,
    asset_id: int,
    *,
    force: bool = False,
    dry_run: bool = False,
    profile_version: str = VISUAL_PROFILE_VERSION,
    visual_caller: VisualCaller | None = None,
) -> Asset:
    asset = session.scalar(
        select(Asset).where(Asset.id == asset_id).options(selectinload(Asset.source))
    )
    if asset is None:
        raise ValueError(f"Asset not found: {asset_id}")
    if not is_visual_candidate(asset):
        return asset
    if not force and asset_visual_is_current(asset, profile_version):
        return asset
    if dry_run:
        return asset

    inputs: VisualFrameInputs | None = None
    try:
        inputs = collect_visual_frames(asset, profile_version)
        caller = visual_caller or call_nvidia_visual_intelligence
        result = caller(build_visual_intelligence_prompt(asset, inputs.sample_timestamps), [inputs.contact_sheet_path])
        apply_visual_intelligence_result(session, asset, result, inputs, profile_version)
        session.commit()
        return asset
    except Exception as exc:
        session.rollback()
        failed = session.get(Asset, asset_id)
        if failed is None:
            raise
        apply_failed_visual_intelligence_result(session, failed, profile_version, exc)
        session.commit()
        return failed
    finally:
        if inputs is not None:
            shutil.rmtree(inputs.temp_dir, ignore_errors=True)


def run_visual_intelligence_backfill(
    session: Session,
    *,
    limit: int | None = None,
    batch_size: int = 20,
    apply: bool = False,
    force: bool = False,
    title_slug: str | None = None,
    profile_version: str = VISUAL_PROFILE_VERSION,
    visual_caller: VisualCaller | None = None,
) -> VisualBackfillResult:
    bounded_batch_size = max(1, min(batch_size, 200))
    selected = processed = skipped = failed = 0
    remaining_limit = limit if limit is not None else None
    seen_asset_ids: set[int] = set()

    while remaining_limit is None or remaining_limit > 0:
        current_limit = bounded_batch_size if remaining_limit is None else min(bounded_batch_size, remaining_limit)
        ids = [
            asset_id
            for asset_id in select_visual_candidate_ids(
                session,
                limit=current_limit + len(seen_asset_ids),
                force=force,
                title_slug=title_slug,
                profile_version=profile_version,
            )
            if asset_id not in seen_asset_ids
        ][:current_limit]
        if not ids:
            break
        seen_asset_ids.update(ids)
        selected += len(ids)
        if remaining_limit is not None:
            remaining_limit -= len(ids)
        if not apply:
            skipped += len(ids)
            if remaining_limit is None or remaining_limit <= 0:
                break
            continue
        for asset_id in ids:
            before = session.get(Asset, asset_id)
            try:
                asset = analyze_asset_visual_intelligence(
                    session,
                    asset_id,
                    force=force,
                    profile_version=profile_version,
                    visual_caller=visual_caller,
                )
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001
                session.rollback()
                failed += 1
                continue
            if before is not None and latest_visual_analysis(asset, profile_version) is not None:
                processed += 1
                if visual_status_from_asset(asset, profile_version) == "failed":
                    failed += 1
            else:
                skipped += 1

    return VisualBackfillResult(
        dry_run=not apply,
        profile_version=profile_version,
        limit=limit,
        batch_size=bounded_batch_size,
        selected=selected,
        processed=processed,
        skipped=skipped,
        failed=failed,
        remaining=count_remaining_visual_assets(
            session,
            profile_version=profile_version,
            force=False,
            title_slug=title_slug,
        ),
        title_slug=title_slug,
    )


def reprocess_visual_assets(
    session: Session,
    asset_ids: list[int],
    *,
    apply: bool = False,
    profile_version: str = VISUAL_PROFILE_VERSION,
    visual_caller: VisualCaller | None = None,
) -> VisualBackfillResult:
    selected = processed = skipped = failed = 0
    for asset_id in asset_ids:
        asset = session.get(Asset, asset_id)
        if asset is None or not is_visual_candidate(asset):
            skipped += 1
            continue
        selected += 1
        if not apply:
            skipped += 1
            continue
        analyzed = analyze_asset_visual_intelligence(
            session,
            asset_id,
            force=True,
            profile_version=profile_version,
            visual_caller=visual_caller,
        )
        processed += 1
        if visual_status_from_asset(analyzed, profile_version) == "failed":
            failed += 1
    return VisualBackfillResult(
        dry_run=not apply,
        profile_version=profile_version,
        limit=len(asset_ids),
        batch_size=len(asset_ids) or 1,
        selected=selected,
        processed=processed,
        skipped=skipped,
        failed=failed,
        remaining=count_remaining_visual_assets(session, profile_version=profile_version, force=False),
    )


def visual_intelligence_status(
    session: Session,
    *,
    profile_version: str = VISUAL_PROFILE_VERSION,
) -> dict[str, Any]:
    total_candidates = session.scalar(select(func.count()).select_from(Asset).where(visual_candidate_filter())) or 0
    latest_analysis_ids = latest_visual_analysis_ids(profile_version)
    processed = (
        session.scalar(
            select(func.count())
            .select_from(AssetAIAnalysis)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(AssetAIAnalysis.result_json["status"].as_string().in_(VISUAL_TERMINAL_STATUSES))
        )
        or 0
    )
    failed = (
        session.scalar(
            select(func.count())
            .select_from(AssetAIAnalysis)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(AssetAIAnalysis.result_json["status"].as_string() == "failed")
        )
        or 0
    )
    remaining = count_remaining_visual_assets(session, profile_version=profile_version, force=False)
    return {
        "total_candidates": total_candidates,
        "pipeline": {
            "profile_version": profile_version,
            "processed": processed,
            "failed": failed,
            "remaining": remaining,
        },
    }


def select_visual_candidate_ids(
    session: Session,
    *,
    limit: int,
    force: bool = False,
    title_slug: str | None = None,
    profile_version: str = VISUAL_PROFILE_VERSION,
) -> list[int]:
    query = select(Asset.id).where(visual_candidate_filter())
    if title_slug is not None:
        query = query.where(Asset.title_slug == title_slug)
    query = query.order_by(Asset.id.asc()).limit(limit)
    if not force:
        latest_analysis_ids = latest_visual_analysis_ids(profile_version)
        current_assets = (
            select(AssetAIAnalysis.asset_id)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(
                AssetAIAnalysis.prompt_version == profile_version,
                AssetAIAnalysis.input_type == VISUAL_ANALYSIS_TYPE,
                AssetAIAnalysis.model == get_settings().nvidia_visual_model,
                AssetAIAnalysis.result_json["status"].as_string() == "ready",
            )
        )
        query = query.where(Asset.id.not_in(current_assets))
    return list(session.scalars(query))


def count_remaining_visual_assets(
    session: Session,
    *,
    profile_version: str = VISUAL_PROFILE_VERSION,
    force: bool = False,
    title_slug: str | None = None,
) -> int:
    query = select(func.count()).select_from(Asset).where(visual_candidate_filter())
    if title_slug is not None:
        query = query.where(Asset.title_slug == title_slug)
    if not force:
        latest_analysis_ids = latest_visual_analysis_ids(profile_version)
        current_assets = (
            select(AssetAIAnalysis.asset_id)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(
                AssetAIAnalysis.prompt_version == profile_version,
                AssetAIAnalysis.input_type == VISUAL_ANALYSIS_TYPE,
                AssetAIAnalysis.model == get_settings().nvidia_visual_model,
                AssetAIAnalysis.result_json["status"].as_string() == "ready",
            )
        )
        query = query.where(Asset.id.not_in(current_assets))
    return int(session.scalar(query) or 0)


def visual_candidate_filter():
    return and_(
        Asset.status == "ready",
        Asset.move_status == "moved",
        Asset.source_status == "active",
        Asset.type == "video",
    )


def is_visual_candidate(asset: Asset) -> bool:
    return (
        asset.status == "ready"
        and asset.move_status == "moved"
        and asset.source_status == "active"
        and asset.type == "video"
    )


def asset_visual_is_current(asset: Asset, profile_version: str) -> bool:
    analysis = latest_visual_analysis(asset, profile_version)
    if analysis is None:
        return False
    result = analysis.result_json or {}
    return (
        analysis.model == get_settings().nvidia_visual_model
        and result.get("status") == "ready"
        and result.get("source_fingerprint") == source_fingerprint(asset)
    )


def latest_visual_analysis(asset: Asset, profile_version: str = VISUAL_PROFILE_VERSION) -> AssetAIAnalysis | None:
    analyses = [
        analysis
        for analysis in asset.ai_analyses
        if analysis.input_type == VISUAL_ANALYSIS_TYPE and analysis.prompt_version == profile_version
    ]
    return max(analyses, key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC), default=None)


def latest_visual_analysis_ids(profile_version: str):
    return (
        select(func.max(AssetAIAnalysis.id).label("analysis_id"))
        .where(
            AssetAIAnalysis.prompt_version == profile_version,
            AssetAIAnalysis.input_type == VISUAL_ANALYSIS_TYPE,
        )
        .group_by(AssetAIAnalysis.asset_id)
        .subquery()
    )


def visual_status_from_asset(asset: Asset, profile_version: str) -> str | None:
    analysis = latest_visual_analysis(asset, profile_version)
    result = analysis.result_json if analysis is not None else None
    if isinstance(result, dict):
        status = result.get("status")
        return str(status) if status else None
    return None


def collect_visual_frames(asset: Asset, profile_version: str = VISUAL_PROFILE_VERSION) -> VisualFrameInputs:
    temp_dir = VISUAL_TEMP_ROOT / "masters" / str(asset.id)
    shutil.rmtree(temp_dir, ignore_errors=True)
    master_path = temp_dir / safe_asset_uid(asset.filename or f"asset-{asset.id}.mp4")
    frame_cache_dir = visual_frame_cache_dir(asset.id, profile_version)
    fingerprint = source_fingerprint(asset)
    cached_frames, cached_timestamps = read_cached_visual_frames(frame_cache_dir, profile_version, fingerprint)
    if len(cached_frames) >= MIN_CACHED_VISUAL_FRAMES:
        contact_sheet = ensure_visual_contact_sheet(cached_frames, frame_cache_dir)
        return VisualFrameInputs(
            master_path=master_path,
            frame_paths=cached_frames,
            contact_sheet_path=contact_sheet,
            frame_cache_dir=frame_cache_dir,
            temp_dir=temp_dir,
            sample_timestamps=cached_timestamps,
        )

    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        location = resolve_asset_rclone_location(asset)
    except AssetLocationError as exc:
        raise VisualIntelligenceOperationalError(str(exc)) from exc
    try:
        RcloneService().copyto(location.remote, location.remote_path, str(master_path))
        if not master_path.is_file() or master_path.stat().st_size <= 0:
            raise VisualIntelligenceOperationalError("asset master download failed")

        shutil.rmtree(frame_cache_dir, ignore_errors=True)
        frame_cache_dir.mkdir(parents=True, exist_ok=True)
        frames = extract_temporal_frames(master_path, frame_cache_dir, asset.duration_seconds, VISUAL_FRAME_COUNT)
        if len(frames) < MIN_CACHED_VISUAL_FRAMES:
            raise VisualIntelligenceOperationalError("visual intelligence requires at least 8 temporal frames")
        write_visual_frame_manifest(frame_cache_dir, profile_version, fingerprint, asset.duration_seconds, frames)
        contact_sheet = ensure_visual_contact_sheet(frames, frame_cache_dir)
        return VisualFrameInputs(
            master_path=master_path,
            frame_paths=frames,
            contact_sheet_path=contact_sheet,
            frame_cache_dir=frame_cache_dir,
            temp_dir=temp_dir,
            sample_timestamps=visual_frame_timestamps(len(frames), asset.duration_seconds),
        )
    finally:
        master_path.unlink(missing_ok=True)


def visual_frame_cache_dir(asset_id: int, profile_version: str = VISUAL_PROFILE_VERSION) -> Path:
    return Path(get_settings().pilot_preview_root) / str(asset_id) / f"{profile_version}-frames"


def read_cached_visual_frames(
    frame_cache_dir: Path, profile_version: str, fingerprint: str
) -> tuple[list[Path], list[float | None]]:
    manifest_path = frame_cache_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    if manifest.get("profile_version") != profile_version or manifest.get("source_fingerprint") != fingerprint:
        return [], []
    frames: list[Path] = []
    timestamps: list[float | None] = []
    for item in manifest.get("frames") or []:
        if not isinstance(item, dict):
            continue
        filename = item.get("file")
        if not isinstance(filename, str) or "/" in filename or "\\" in filename:
            continue
        frame_path = frame_cache_dir / filename
        if is_valid_visual_frame(frame_path):
            frames.append(frame_path)
            timestamp = item.get("timestamp")
            timestamps.append(float(timestamp) if isinstance(timestamp, int | float) else None)
    return frames, timestamps


def is_valid_visual_frame(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, UnidentifiedImageError):
        return False


def ensure_visual_contact_sheet(frame_paths: list[Path], frame_cache_dir: Path) -> Path:
    contact_sheet = frame_cache_dir / CONTACT_SHEET_FILENAME
    if is_valid_contact_sheet(contact_sheet):
        return contact_sheet
    create_visual_contact_sheet(frame_paths, contact_sheet)
    if not is_valid_contact_sheet(contact_sheet):
        raise VisualIntelligenceOperationalError("visual contact sheet could not be created")
    return contact_sheet


def is_valid_contact_sheet(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.width > 0 and image.height > 0
    except (OSError, UnidentifiedImageError):
        return False


def create_visual_contact_sheet(frame_paths: list[Path], output_path: Path) -> None:
    frames: list[Image.Image] = []
    try:
        for path in frame_paths:
            with Image.open(path) as image:
                frames.append(image.convert("RGB").copy())
        if not frames:
            raise VisualIntelligenceOperationalError("visual contact sheet requires frames")
        columns = 5 if len(frames) >= 5 else len(frames)
        rows = (len(frames) + columns - 1) // columns
        tile_width = max(1, CONTACT_SHEET_MAX_DIMENSION // columns)
        median_aspect = sorted(image.height / image.width for image in frames)[len(frames) // 2]
        tile_height = max(1, round(tile_width * median_aspect))
        if tile_height * rows > CONTACT_SHEET_MAX_DIMENSION:
            scale = CONTACT_SHEET_MAX_DIMENSION / (tile_height * rows)
            tile_width = max(1, int(tile_width * scale))
            tile_height = max(1, int(tile_height * scale))
        sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), (0, 0, 0))
        for index, image in enumerate(frames):
            resized = fit_frame_to_tile(image, tile_width, tile_height)
            x = (index % columns) * tile_width + (tile_width - resized.width) // 2
            y = (index // columns) * tile_height + (tile_height - resized.height) // 2
            sheet.paste(resized, (x, y))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output_path, format="JPEG", quality=88, optimize=True)
    finally:
        for frame in frames:
            frame.close()


def fit_frame_to_tile(image: Image.Image, tile_width: int, tile_height: int) -> Image.Image:
    scale = min(tile_width / image.width, tile_height / image.height)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def write_visual_frame_manifest(
    frame_cache_dir: Path,
    profile_version: str,
    fingerprint: str,
    duration_seconds: float | None,
    frames: list[Path],
) -> None:
    frame_count = len(frames)
    duration = duration_seconds if duration_seconds and duration_seconds > 0 else None
    manifest = {
        "profile_version": profile_version,
        "source_fingerprint": fingerprint,
        "duration_seconds": duration,
        "frames": [
            {
                "file": path.name,
                "timestamp": estimated_frame_timestamp(index, frame_count, duration),
            }
            for index, path in enumerate(frames, start=1)
        ],
    }
    (frame_cache_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def visual_frame_timestamps(frame_count: int, duration_seconds: float | None) -> list[float | None]:
    return [
        estimated_frame_timestamp(index, frame_count, duration_seconds)
        for index in range(1, frame_count + 1)
    ]


def estimated_frame_timestamp(index: int, frame_count: int, duration_seconds: float | None) -> float | None:
    if not duration_seconds or duration_seconds <= 0 or frame_count <= 0:
        return None
    ratio = index / (frame_count + 1)
    return round(max(0.0, min(duration_seconds * ratio, duration_seconds - 0.05)), 3)


def extract_temporal_frames(
    master_path: Path,
    output_dir: Path,
    duration_seconds: float | None,
    frame_count: int = VISUAL_FRAME_COUNT,
) -> list[Path]:
    frame_count = max(8, min(frame_count, 12))
    duration = duration_seconds if duration_seconds and duration_seconds > 0 else probe_duration_seconds(master_path)
    if not duration or duration <= 0:
        return []
    frames: list[Path] = []
    ratios = [(index + 1) / (frame_count + 1) for index in range(frame_count)]
    for index, ratio in enumerate(ratios, start=1):
        timestamp = max(0.0, min(duration * ratio, duration - 0.05))
        output = output_dir / f"frame_{index:03d}.jpg"
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(master_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=768:-2",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            frames.append(output)
    return frames


def probe_duration_seconds(path: Path) -> float | None:
    try:
        parsed = parse_ffprobe_metadata(run_ffprobe(path))
    except Exception:  # noqa: BLE001
        return None
    return parsed.duration_seconds


def call_nvidia_visual_intelligence(
    prompt: str,
    image_paths: list[Path],
) -> VisualIntelligenceResult:
    settings = get_settings()
    if not settings.nvidia_api_key:
        raise VisualIntelligenceOperationalError("NVIDIA_API_KEY is not configured")
    attempt_count = 0
    text, attempt_count = post_visual_completion_with_retries(prompt, image_paths, settings, attempt_count)
    try:
        result, normalization_warnings = validate_visual_payload_text(text)
    except (VisualPayloadJSONError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            repair_prompt = (
                prompt
                + "\n\nThe previous response failed schema validation.\n"
                + "Return the COMPLETE JSON object again.\n"
                + "Do not omit required fields.\n"
                + "Validation problems:\n"
                + summarize_pydantic_errors(exc)
            )
        else:
            repair_prompt = (
                prompt
                + "\n\nThe previous response was not valid JSON.\n"
                + "Return the COMPLETE visual-v1 JSON object only.\n"
                + "No markdown, no code fences, no commentary."
            )
        try:
            text, attempt_count = post_visual_completion_with_retries(
                repair_prompt,
                image_paths,
                settings,
                attempt_count,
            )
            result, normalization_warnings = validate_visual_payload_text(text)
        except ValidationError as repair_exc:
            error = VisualIntelligenceOperationalError("NVIDIA returned visual-v1 JSON that failed schema validation")
            object.__setattr__(error, "_visual_attempt_count", attempt_count)
            object.__setattr__(error, "_visual_final_error", summarize_pydantic_errors(repair_exc))
            object.__setattr__(error, "_visual_retryable", True)
            raise error from repair_exc
        except VisualPayloadJSONError as repair_exc:
            error = VisualIntelligenceOperationalError("NVIDIA returned invalid visual-v1 JSON")
            object.__setattr__(error, "_visual_attempt_count", attempt_count)
            object.__setattr__(error, "_visual_final_error", sanitize_provider_error(str(repair_exc)))
            object.__setattr__(error, "_visual_retryable", True)
            raise error from repair_exc
        except Exception as repair_exc:
            raise VisualIntelligenceOperationalError("NVIDIA returned invalid visual-v1 JSON") from repair_exc
    except Exception as exc:
        raise VisualIntelligenceOperationalError("NVIDIA returned invalid visual-v1 JSON") from exc
    object.__setattr__(result, "_normalization_warnings", normalization_warnings)
    object.__setattr__(result, "_attempt_count", attempt_count)
    object.__setattr__(result, "_final_error", None)
    return result


def post_visual_completion_with_retries(
    prompt: str,
    image_paths: list[Path],
    settings: Any,
    attempt_count: int,
) -> tuple[str, int]:
    phase_attempt_count = 0
    while True:
        phase_attempt_count += 1
        attempt_count += 1
        try:
            text = post_chat_completion(
                prompt,
                image_paths,
                settings.nvidia_visual_model,
                timeout_seconds=90.0,
                max_tokens=VISUAL_V1_NVIDIA_MAX_TOKENS,
            )
            return text, attempt_count
        except NvidiaProviderError as exc:
            if phase_attempt_count > len(VISUAL_NVIDIA_RETRY_DELAYS) or not is_transient_nvidia_error(exc):
                object.__setattr__(exc, "_visual_attempt_count", attempt_count)
                object.__setattr__(exc, "_visual_final_error", sanitize_provider_error(str(exc)))
                raise
            delay = VISUAL_NVIDIA_RETRY_DELAYS[phase_attempt_count - 1]
            time.sleep(random.uniform(delay * 0.8, delay * 1.2))


def validate_visual_payload_text(text: str) -> tuple[VisualIntelligenceResult, list[str]]:
    try:
        payload = json.loads(extract_json_object(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise VisualPayloadJSONError("NVIDIA returned invalid visual-v1 JSON") from exc
    payload, normalization_warnings = normalize_visual_payload(payload)
    return VisualIntelligenceResult.model_validate(payload), normalization_warnings


def summarize_pydantic_errors(exc: ValidationError, *, limit: int = 6) -> str:
    problems: list[str] = []
    for error in exc.errors()[:limit]:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        message = str(error.get("msg") or error.get("type") or "validation error")
        problems.append(f"- {loc}: {message}")
    remaining = len(exc.errors()) - len(problems)
    if remaining > 0:
        problems.append(f"- ... {remaining} more validation problem(s)")
    return "\n".join(problems)


def is_transient_nvidia_error(exc: NvidiaProviderError) -> bool:
    cause = exc.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        return cause.code in TRANSIENT_NVIDIA_HTTP_STATUSES
    if isinstance(cause, TimeoutError | socket.timeout):
        return True
    message = str(exc).lower()
    if "timed out" in message or "timeout" in message:
        return True
    return any(f"http {status}" in message for status in TRANSIENT_NVIDIA_HTTP_STATUSES)


def normalize_visual_payload(payload: Any) -> tuple[Any, list[str]]:
    if not isinstance(payload, dict):
        return payload, []
    normalized = deepcopy(payload)
    warnings: list[str] = []
    semantics = normalized.get("semantics")
    if isinstance(semantics, dict) and "visible_text" in semantics:
        visible_text = semantics.get("visible_text")
        if isinstance(visible_text, str):
            semantics["visible_text"] = visible_text.strip()
        elif isinstance(visible_text, list):
            clean_text_items: list[str] = []
            seen_text_items: set[str] = set()
            for item in visible_text:
                if not isinstance(item, str):
                    continue
                clean_item = item.strip()
                if not clean_item or clean_item in seen_text_items:
                    continue
                clean_text_items.append(clean_item)
                seen_text_items.add(clean_item)
            semantics["visible_text"] = (
                " | ".join(clean_text_items) if clean_text_items else None
            )
            warnings.append("semantics.visible_text_list_normalized")
        elif visible_text is not None:
            semantics["visible_text"] = None
            warnings.append("semantics.visible_text_invalid_dropped")

    composition = normalized.get("composition")
    if isinstance(composition, dict):
        if "safe_text_areas" in composition:
            safe_text_areas = composition.get("safe_text_areas")
            clean_safe_text_areas: list[str] = []
            if safe_text_areas is None:
                composition["safe_text_areas"] = []
            elif isinstance(safe_text_areas, str):
                clean_area = safe_text_areas.strip()
                composition["safe_text_areas"] = (
                    [] if clean_area.lower() == "none" else ([clean_area] if clean_area else [])
                )
                warnings.append("composition.safe_text_areas_string_normalized")
            elif isinstance(safe_text_areas, list):
                valid_safe_text_areas = {"top", "middle", "bottom", "left", "right", "center", "none"}
                seen_areas: set[str] = set()
                for item in safe_text_areas:
                    if not isinstance(item, str):
                        continue
                    clean_area = item.strip()
                    if clean_area not in valid_safe_text_areas or clean_area in seen_areas:
                        continue
                    clean_safe_text_areas.append(clean_area)
                    seen_areas.add(clean_area)
                composition["safe_text_areas"] = clean_safe_text_areas

        subject_region = composition.get("subject_region")
        if subject_region is not None:
            clean_region = normalized_float_list(subject_region, 4)
            if clean_region is None:
                composition["subject_region"] = None
                warnings.append("composition.subject_region_dropped")
            else:
                composition["subject_region"] = clean_region

        subject_trajectory = composition.get("subject_trajectory")
        if subject_trajectory is not None:
            clean_trajectory = normalized_subject_trajectory(subject_trajectory)
            if clean_trajectory is None:
                composition["subject_trajectory"] = []
                warnings.append("composition.subject_trajectory_dropped")
            else:
                if len(clean_trajectory) != len(subject_trajectory):
                    warnings.append("composition.subject_trajectory_invalid_items_dropped")
                composition["subject_trajectory"] = clean_trajectory

    transforms = normalized.get("transforms")
    if isinstance(transforms, dict):
        zoom = transforms.get("zoom")
        if isinstance(zoom, dict) and "max_safe_zoom" in zoom:
            clean_zoom = normalized_float(zoom.get("max_safe_zoom"))
            if clean_zoom is None:
                zoom["max_safe_zoom"] = 1.0
                warnings.append("transforms.zoom.max_safe_zoom_invalid_normalized")
            elif clean_zoom < 1.0:
                zoom["max_safe_zoom"] = 1.0
                warnings.append("transforms.zoom.max_safe_zoom_below_identity_normalized")
            else:
                zoom["max_safe_zoom"] = clean_zoom
        pan = transforms.get("pan")
        if isinstance(pan, dict):
            if isinstance(pan.get("safe_directions"), list):
                direction_mapping = {
                    "top": "up",
                    "bottom": "down",
                    "left": "left",
                    "right": "right",
                    "up": "up",
                    "down": "down",
                }
                clean_directions: list[str] = []
                seen_directions: set[str] = set()
                dropped_unknown_direction = False
                for item in pan["safe_directions"]:
                    if not isinstance(item, str):
                        dropped_unknown_direction = True
                        continue
                    mapped = direction_mapping.get(item.strip().lower())
                    if mapped is None:
                        dropped_unknown_direction = True
                        continue
                    if mapped not in seen_directions:
                        clean_directions.append(mapped)
                        seen_directions.add(mapped)
                if clean_directions != pan["safe_directions"]:
                    warnings.append("transforms.pan.safe_directions_normalized")
                if dropped_unknown_direction:
                    warnings.append("transforms.pan.safe_directions_unknown_dropped")
                pan["safe_directions"] = clean_directions
            for key in ("max_offset_x", "max_offset_y"):
                if key in pan and pan[key] is not None:
                    clean_offset = normalized_float(pan[key], minimum=0.0, maximum=1.0)
                    if clean_offset is None:
                        pan[key] = None
                        warnings.append(f"transforms.pan.{key}_dropped")
                    else:
                        pan[key] = clean_offset
        for key in ("crop_vertical", "crop_horizontal", "crop"):
            crop = transforms.get(key)
            if isinstance(crop, dict):
                safe_rect = crop.get("safe_rect")
                if safe_rect is not None:
                    clean_rect = normalized_float_list(safe_rect, 4)
                    if clean_rect is None:
                        crop["safe_rect"] = None
                        warnings.append(f"transforms.{key}.safe_rect_dropped")
                    else:
                        crop["safe_rect"] = clean_rect
    return normalized, warnings


def normalized_subject_trajectory(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    points: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        timestamp = normalized_float(item.get("timestamp"), minimum=0.0)
        center = normalized_float_list(item.get("center"), 2)
        bbox = normalized_float_list(item.get("bbox"), 4)
        if timestamp is None or center is None or bbox is None:
            continue
        points.append({"timestamp": timestamp, "center": center, "bbox": bbox})
    return points


def normalized_float_list(value: Any, length: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != length:
        return None
    items: list[float] = []
    for item in value:
        number = normalized_float(item)
        if number is None:
            return None
        items.append(number)
    return items


def normalized_float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def decide_visual_editorial_status(result: VisualIntelligenceResult) -> VisualEditorialDecision:
    garbage = result.garbage
    reason_codes: list[str] = []
    hard_flags = [flag for flag in HARD_REJECT_FLAGS if getattr(garbage, flag)]
    quarantine_flags = [flag for flag in QUARANTINE_FLAGS if getattr(garbage, flag)]

    if hard_flags and result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return VisualEditorialDecision("rejected", dedupe_codes(hard_flags))
    if not garbage.editorial_usable and result.confidence >= HIGH_CONFIDENCE_THRESHOLD and garbage.score >= 0.75:
        return VisualEditorialDecision("rejected", ["not_editorial_usable"])

    reason_codes.extend(hard_flags)
    reason_codes.extend(quarantine_flags)
    if result.confidence < LOW_CONFIDENCE_THRESHOLD:
        reason_codes.append("low_confidence")
    if result.quality.score < 0.55 or result.quality.label in {"weak", "bad"}:
        reason_codes.append("low_editorial_quality")
    if garbage.is_garbage or garbage.score >= 0.55:
        reason_codes.append("garbage_risk")
    if not garbage.editorial_usable:
        reason_codes.append("not_editorial_usable")
    if result.needs_human_review:
        reason_codes.append("needs_human_review")
    if reason_codes:
        return VisualEditorialDecision("quarantined", dedupe_codes(reason_codes))
    return VisualEditorialDecision("searchable", [])


def normalize_visual_transforms(result: VisualIntelligenceResult, asset: Asset) -> NormalizedVisualTransforms:
    garbage = result.garbage
    semantics = result.semantics
    composition = result.composition
    consistency_warnings: list[str] = []
    flip_blockers: list[str] = []
    if semantics.visible_text:
        flip_blockers.append("visible_text")
    if garbage.logo or semantics.logo_or_watermark:
        flip_blockers.append("logo")
    if garbage.watermark or semantics.logo_or_watermark:
        flip_blockers.append("watermark")
    if garbage.social_media_ui:
        flip_blockers.append("social_media_ui")
    if garbage.subscribe_cta:
        flip_blockers.append("subscribe_cta")
    if has_directional_signal(result):
        flip_blockers.append("directionality")
    if has_lateral_semantic_risk(result.transforms.flip.risk_reasons):
        flip_blockers.append("lateral_semantic_meaning")
    if (
        not result.transforms.flip.allowed
        and result.transforms.flip.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and not result.transforms.flip.risk_reasons
        and not flip_blockers
    ):
        consistency_warnings.append("transforms.flip.allowed_false_high_confidence_empty_risk_reasons")
    flip_decision = normalized_basic_decision(
        requested_allowed=result.transforms.flip.allowed,
        confidence=result.transforms.flip.confidence,
        blockers=flip_blockers,
        reasons=result.transforms.flip.risk_reasons,
    )

    max_safe_zoom = min(float(result.transforms.zoom.max_safe_zoom), MAX_VISUAL_V1_ZOOM)
    zoom_blockers: list[str] = []
    zoom_reasons = risk_reasons_only(result.transforms.zoom.risk_reasons)
    if not result.transforms.zoom.allowed and result.transforms.zoom.max_safe_zoom > 1.0:
        consistency_warnings.append("transforms.zoom.allowed_false_with_safe_zoom")
    if (
        not result.transforms.zoom.allowed
        and result.transforms.zoom.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and not zoom_reasons
    ):
        consistency_warnings.append("transforms.zoom.allowed_false_high_confidence_empty_risk_reasons")
    if result.transforms.zoom.allowed and max_safe_zoom <= 1.0:
        consistency_warnings.append("transforms.zoom.allowed_true_without_safe_zoom")
    if composition.edge_proximity >= 0.80 or garbage.subject_badly_clipped:
        max_safe_zoom = 1.0
        if garbage.subject_badly_clipped:
            zoom_blockers.append("subject_badly_clipped")
        if composition.edge_proximity >= 0.80:
            zoom_blockers.append("subject_dangerously_near_edge")
    elif composition.edge_proximity >= 0.65:
        max_safe_zoom = min(max_safe_zoom, 1.03)
        zoom_reasons.append("edge_subject")
    smallest_side = min(asset.width or 0, asset.height or 0)
    if smallest_side and smallest_side < 480:
        max_safe_zoom = 1.0
        zoom_blockers.append("insufficient_resolution")
    elif smallest_side and smallest_side < 720:
        max_safe_zoom = min(max_safe_zoom, 1.03)
    zoom_decision_base = normalized_basic_decision(
        requested_allowed=result.transforms.zoom.allowed and max_safe_zoom > 1.0,
        confidence=result.transforms.zoom.confidence,
        blockers=zoom_blockers,
        reasons=zoom_reasons,
    )
    zoom_decision = NormalizedZoomDecision(
        status=zoom_decision_base.status,
        allowed=zoom_decision_base.allowed,
        confidence=zoom_decision_base.confidence,
        blockers=zoom_decision_base.blockers,
        reasons=zoom_decision_base.reasons,
        max_safe_zoom=round(max(1.0, min(max_safe_zoom if zoom_decision_base.allowed else 1.0, MAX_VISUAL_V1_ZOOM)), 3),
    )

    pan_blockers: list[str] = []
    pan_reasons = risk_reasons_only(result.transforms.pan.risk_reasons)
    pan_safe_directions = list(result.transforms.pan.safe_directions)
    if result.transforms.pan.allowed and not pan_safe_directions:
        consistency_warnings.append("transforms.pan.allowed_true_empty_safe_directions")
        pan_reasons.append("geometry_or_trajectory_unavailable")
    if (
        not result.transforms.pan.allowed
        and result.transforms.pan.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and not pan_reasons
    ):
        consistency_warnings.append("transforms.pan.allowed_false_high_confidence_empty_risk_reasons")
    if composition.camera_motion not in {"static", "unknown"}:
        pan_blockers.append("existing_camera_motion")
    if high_subject_trajectory_motion(result):
        pan_blockers.append("high_subject_motion")
    if composition.negative_space < 0.20 or composition.edge_proximity > 0.55:
        pan_blockers.append("insufficient_composition_margin")
    if composition.camera_motion == "unknown" or composition.camera_motion_confidence < LOW_CONFIDENCE_THRESHOLD:
        pan_reasons.append("geometry_or_trajectory_unavailable")
    pan_decision_base = normalized_basic_decision(
        requested_allowed=result.transforms.pan.allowed and bool(pan_safe_directions),
        confidence=min(result.transforms.pan.confidence, composition.camera_motion_confidence),
        blockers=pan_blockers,
        reasons=pan_reasons,
    )
    pan_max_offset_x = min_optional(result.transforms.pan.max_offset_x, 0.08) if pan_decision_base.allowed else 0.0
    pan_max_offset_y = min_optional(result.transforms.pan.max_offset_y, 0.08) if pan_decision_base.allowed else 0.0
    pan_decision = NormalizedPanDecision(
        status=pan_decision_base.status,
        allowed=pan_decision_base.allowed,
        confidence=pan_decision_base.confidence,
        blockers=pan_decision_base.blockers,
        reasons=pan_decision_base.reasons,
        safe_directions=pan_safe_directions if pan_decision_base.allowed else [],
        max_offset_x=pan_max_offset_x,
        max_offset_y=pan_max_offset_y,
    )

    if result.transforms.crop_vertical.allowed and result.transforms.crop_vertical.safe_rect is None:
        consistency_warnings.append("transforms.crop_vertical.allowed_true_null_safe_rect")
    if (
        not result.transforms.crop_vertical.allowed
        and result.transforms.crop_vertical.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and not result.transforms.crop_vertical.risk_reasons
        and not composition.crop_risk_reasons
    ):
        consistency_warnings.append("transforms.crop_vertical.allowed_false_high_confidence_empty_risk_reasons")
    crop_vertical_decision = normalized_crop_decision(
        allowed=result.transforms.crop_vertical.allowed,
        safe_rect=result.transforms.crop_vertical.safe_rect,
        confidence=result.transforms.crop_vertical.confidence,
        reasons=[*result.transforms.crop_vertical.risk_reasons, *composition.crop_risk_reasons],
    )
    if result.transforms.crop_horizontal.allowed and result.transforms.crop_horizontal.safe_rect is None:
        consistency_warnings.append("transforms.crop_horizontal.allowed_true_null_safe_rect")
    if (
        not result.transforms.crop_horizontal.allowed
        and result.transforms.crop_horizontal.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and not result.transforms.crop_horizontal.risk_reasons
        and not composition.crop_risk_reasons
    ):
        consistency_warnings.append("transforms.crop_horizontal.allowed_false_high_confidence_empty_risk_reasons")
    crop_horizontal_decision = normalized_crop_decision(
        allowed=result.transforms.crop_horizontal.allowed,
        safe_rect=result.transforms.crop_horizontal.safe_rect,
        confidence=result.transforms.crop_horizontal.confidence,
        reasons=[*result.transforms.crop_horizontal.risk_reasons, *composition.crop_risk_reasons],
    )

    return NormalizedVisualTransforms(
        flip_horizontal=flip_decision,
        zoom=zoom_decision,
        pan=pan_decision,
        crop_vertical=crop_vertical_decision,
        crop_horizontal=crop_horizontal_decision,
        safe_text_areas=composition.safe_text_areas,
        consistency_warnings=dedupe_codes(consistency_warnings),
    )


def normalized_basic_decision(
    *,
    requested_allowed: bool,
    confidence: float,
    blockers: list[str],
    reasons: list[str],
) -> NormalizedTransformDecision:
    clean_blockers = dedupe_codes(blockers)
    clean_reasons = dedupe_codes([*risk_reasons_only(reasons), *clean_blockers])
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        clean_reasons = dedupe_codes([*clean_reasons, "low_confidence"])
    if clean_blockers:
        return NormalizedTransformDecision(
            status="unsafe",
            allowed=False,
            confidence=round(confidence, 3),
            blockers=clean_blockers,
            reasons=clean_reasons,
        )
    if confidence < LOW_CONFIDENCE_THRESHOLD or not requested_allowed:
        return NormalizedTransformDecision(
            status="unknown",
            allowed=False,
            confidence=round(confidence, 3),
            blockers=[],
            reasons=clean_reasons or ["insufficient_evidence"],
        )
    return NormalizedTransformDecision(
        status="safe",
        allowed=True,
        confidence=round(confidence, 3),
        blockers=[],
        reasons=clean_reasons,
    )


def normalized_crop_decision(
    *,
    allowed: bool,
    safe_rect: list[float] | None,
    confidence: float,
    reasons: list[str],
) -> NormalizedCropDecision:
    crop_reasons = risk_reasons_only(reasons)
    crop_blockers = [reason for reason in crop_reasons if reason in {"cuts_subject", "cuts_text", "cuts_logo", "bad_crop"}]
    if allowed and safe_rect is None:
        crop_reasons.append("geometry_unavailable")
    base = normalized_basic_decision(
        requested_allowed=allowed and safe_rect is not None,
        confidence=confidence,
        blockers=crop_blockers,
        reasons=crop_reasons,
    )
    return NormalizedCropDecision(
        status=base.status,
        allowed=base.allowed,
        confidence=base.confidence,
        blockers=base.blockers,
        reasons=base.reasons,
        safe_rect=safe_rect if base.allowed else None,
    )


def has_lateral_semantic_risk(reasons: list[str]) -> bool:
    lateral_reasons = {
        "handedness",
        "lateral_semantic_meaning",
        "directionality",
        "signage",
        "arrows",
        "numbers",
    }
    return any(dedupe_codes([reason])[0] in lateral_reasons for reason in reasons if dedupe_codes([reason]))


def risk_reasons_only(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if not is_non_risk_transform_reason(reason)]


def is_non_risk_transform_reason(reason: str) -> bool:
    code = dedupe_codes([reason])
    if not code:
        return True
    value = code[0]
    non_risk_patterns = (
        "no_necesita",
        "not_needed",
        "not_necessary",
        "unnecessary",
        "ya_esta_bien",
        "bien_encuadrado",
        "well_framed",
        "already_framed",
        "already_well_composed",
        "no_improvement",
        "does_not_improve",
        "opcional",
        "optional",
        "camara_estatica",
        "camera_static",
        "static_camera",
        "margen_suficiente",
        "sufficient_margin",
        "enough_margin",
        "no_recommend",
        "not_recommended",
    )
    return any(pattern in value for pattern in non_risk_patterns)


def has_directional_signal(result: VisualIntelligenceResult) -> bool:
    directional_terms = ("flecha", "direccion", "señal", "senal", "cartel", "izquierda", "derecha")
    searchable = " ".join(
        [
            *result.semantics.subjects,
            *result.semantics.actions,
            *result.semantics.objects,
            *result.semantics.keywords_es,
            result.semantics.visible_text or "",
        ]
    ).lower()
    return any(term in searchable for term in directional_terms)


def high_subject_trajectory_motion(result: VisualIntelligenceResult) -> bool:
    points = result.composition.subject_trajectory
    if len(points) < 2:
        return False
    first = points[0].center
    last = points[-1].center
    return abs(last[0] - first[0]) + abs(last[1] - first[1]) > 0.35


def min_optional(value: float | None, cap: float) -> float:
    if value is None:
        return cap
    return round(max(0.0, min(float(value), cap)), 3)


def dedupe_codes(values: list[str]) -> list[str]:
    codes: list[str] = []
    for value in values:
        code = str(value or "").strip().lower().replace(" ", "_")
        if code and code not in codes:
            codes.append(code[:80])
    return codes


def normalize_camera_motion(value: str | None) -> str:
    mapping = {
        "static": "static",
        "handheld": "handheld",
        "tracking": "tracking",
        "unknown": "unknown",
        "pan_left": "pan",
        "pan_right": "pan",
        "tilt_up": "tilt",
        "tilt_down": "tilt",
        "zoom_in": "zoom",
        "zoom_out": "zoom",
        "high_motion": "handheld",
    }
    return mapping.get(str(value or "").strip().lower(), "unknown")


def normalize_shot_type(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    mapping = {
        "close_up": "closeup",
        "closeup": "closeup",
        "medium": "medium",
        "wide": "wide",
        "detail": "detail",
        "establishing": "establishing",
    }
    return mapping.get(normalized, "unknown")


def normalize_subject_position(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"center", "left", "right", "top", "bottom", "full_frame"}:
        return normalized
    return "unknown"


def build_visual_intelligence_prompt(
    asset: Asset, sample_timestamps: list[float | None] | None = None
) -> str:
    timestamp_values = [timestamp for timestamp in (sample_timestamps or []) if timestamp is not None]
    timestamp_text = (
        "sample timestamps: ["
        + ", ".join(f"{timestamp:.3f}" for timestamp in timestamp_values)
        + "]\n"
        if timestamp_values
        else ""
    )
    return (
        "Analiza un contact sheet temporal de un master de video para una biblioteca editorial.\n"
        "Esta imagen es una cuadricula cronologica de frames del mismo video, "
        "ordenados de izquierda a derecha y de arriba abajo.\n"
        + timestamp_text
        + "Los timestamps son metadata del sistema y NO son texto visible en el video.\n"
        "Responde exclusivamente con un objeto JSON valido, sin markdown ni explicaciones.\n"
        "No inventes contexto fuera de lo visible. Todo texto libre debe estar en espanol neutro.\n\n"
        "Estructura obligatoria:\n"
        "{\n"
        '  "quality": {\n'
        '    "score": 0.0,\n'
        '    "label": "usable",\n'
        '    "sharpness": 0.0,\n'
        '    "exposure": 0.0,\n'
        '    "stability": 0.0,\n'
        '    "lighting": 0.0,\n'
        '    "temporal_consistency": 0.0,\n'
        '    "reason_codes": []\n'
        "  },\n"
        '  "garbage": {\n'
        '    "is_garbage": REQUIRED_BOOLEAN,\n'
        '    "score": REQUIRED_FLOAT_0_TO_1,\n'
        '    "black_or_blank": REQUIRED_BOOLEAN,\n'
        '    "subject_severely_out_of_frame": REQUIRED_BOOLEAN,\n'
        '    "subject_badly_clipped": REQUIRED_BOOLEAN,\n'
        '    "social_media_ui": REQUIRED_BOOLEAN,\n'
        '    "subscribe_cta": REQUIRED_BOOLEAN,\n'
        '    "emoji_overlay": REQUIRED_BOOLEAN,\n'
        '    "watermark": REQUIRED_BOOLEAN,\n'
        '    "logo": REQUIRED_BOOLEAN,\n'
        '    "heavy_text_overlay": REQUIRED_BOOLEAN,\n'
        '    "nearly_empty": REQUIRED_BOOLEAN,\n'
        '    "severe_blur": REQUIRED_BOOLEAN,\n'
        '    "severe_black_frames": REQUIRED_BOOLEAN,\n'
        '    "corrupted_frames": REQUIRED_BOOLEAN,\n'
        '    "accidental_capture": REQUIRED_BOOLEAN,\n'
        '    "editorial_usable": REQUIRED_BOOLEAN,\n'
        '    "reasons": REQUIRED_ARRAY_OF_REASON_CODES\n'
        "  },\n"
        '  "semantics": {\n'
        '    "summary_es": "",\n'
        '    "subjects": [],\n'
        '    "actions": [],\n'
        '    "objects": [],\n'
        '    "emotions": [],\n'
        '    "narrative_themes": [],\n'
        '    "possible_use_cases": [],\n'
        '    "negative_use_cases": [],\n'
        '    "setting": null,\n'
        '    "mood": null,\n'
        '    "keywords_es": [],\n'
        '    "contains_people": false,\n'
        '    "visible_text": null,\n'
        '    "logo_or_watermark": false\n'
        "  },\n"
        '  "composition": {\n'
        '    "shot_type": "",\n'
        '    "subject_position": "",\n'
        '    "subject_framing": "",\n'
        '    "subject_region": null,\n'
        '    "subject_trajectory": [],\n'
        '    "negative_space": 0.0,\n'
        '    "edge_proximity": 0.0,\n'
        '    "vertical_suitability": 0.0,\n'
        '    "horizontal_suitability": 0.0,\n'
        '    "camera_motion": "unknown",\n'
        '    "camera_motion_confidence": required_float_0_to_1,\n'
        '    "safe_text_areas": [],\n'
        '    "crop_risk_reasons": []\n'
        "  },\n"
        '  "transforms": {\n'
        '    "flip": {"allowed": boolean, "confidence": required_float_0_to_1, "risk_reasons": []},\n'
        '    "zoom": {"allowed": boolean, "max_safe_zoom": float_1_to_3, "confidence": required_float_0_to_1, "risk_reasons": []},\n'
        '    "pan": {"allowed": boolean, "safe_directions": [], "max_offset_x": null_or_float_0_to_1, "max_offset_y": null_or_float_0_to_1, "confidence": required_float_0_to_1, "risk_reasons": []},\n'
        '    "crop_vertical": {"allowed": boolean, "safe_rect": null_or_normalized_rect, "confidence": required_float_0_to_1, "preferred_aspect_ratios": ["9:16"], "risk_reasons": []},\n'
        '    "crop_horizontal": {"allowed": boolean, "safe_rect": null_or_normalized_rect, "confidence": required_float_0_to_1, "preferred_aspect_ratios": ["16:9"], "risk_reasons": []}\n'
        "  },\n"
        '  "confidence": required_float_0_to_1,\n'
        '  "needs_human_review": false,\n'
        '  "review_reason": null\n'
        "}\n\n"
        "Reglas de calculo:\n"
        "- quality.score combina nitidez, exposicion, estabilidad, iluminacion y consistencia temporal.\n"
        "- EVALUA CADA CAMPO DEL BLOQUE GARBAGE DE FORMA INDEPENDIENTE.\n"
        "- No copies valores por defecto: cada flag garbage es REQUIRED true/false segun evidencia del contact sheet.\n"
        "- garbage.score es REQUIRED float real 0.0..1.0. Usa 0.0 solamente si realmente no existe ninguna senal editorial problematica.\n"
        "- garbage.score mide probabilidad de asset inutil: negro, corrupto, borroso extremo o captura accidental.\n"
        "- garbage.editorial_usable es REQUIRED y debe ser una evaluacion real; no asumas true.\n"
        "- visible_text por si solo NO es garbage: texto natural en camiseta, libro, senal, calendario, letrero o packaging puede ser usable.\n"
        "- garbage.heavy_text_overlay=true solo cuando texto/grafica domina, obstruye o contamina editorialmente el plano.\n"
        "- garbage.logo=true solo si hay un logo visible real.\n"
        "- garbage.watermark=true solo si hay marca de agua o branding overlay persistente.\n"
        "- garbage.social_media_ui=true solo si hay UI real TikTok, Instagram, YouTube u otra plataforma.\n"
        "- garbage.subscribe_cta=true solo si hay CTA visible real.\n"
        "- garbage.subject_badly_clipped=true solo para clipping severo, no framing normal.\n"
        "- garbage.accidental_capture=true solo para captura claramente accidental o inservible.\n"
        "- semantics resume sujeto, accion, entorno, mood, texto visible, logos y keywords buscables.\n"
        "- semantics.visible_text debe ser un unico string con todo el texto legible relevante o null. "
        "Si hay varios textos, unelos en ese string; nunca devuelvas array.\n"
        "- garbage contiene senales estructuradas de inutilidad editorial; usa confidence global para calibrarlas.\n"
        "- confidence global es REQUIRED float 0.0..1.0 y debe calibrar la evaluacion visual completa.\n"
        "- composition evalua encuadre, posicion del sujeto, trayectoria opcional, suitability vertical/horizontal, movimiento de camara y zonas seguras de texto.\n"
        "- camera_motion solo puede ser static, pan_left, pan_right, tilt_up, tilt_down, zoom_in, zoom_out, handheld, tracking, high_motion o unknown.\n"
        "- camera_motion_confidence es REQUIRED float 0.0..1.0 y debe estimar evidencia real del movimiento de camara.\n"
        "- transforms.*.allowed significa SEGURIDAD/CAPACIDAD: true si la transformacion puede aplicarse conservadoramente sin danar contenido importante ni introducir errores visuales o semanticos; false solo si existe evidencia de que NO es seguro aplicarla.\n"
        "- No uses allowed=false porque el transform no hace falta, el plano ya esta bien compuesto, no mejora el plano, seria opcional o no lo elegirias editorialmente. MPT decide si aplicar transforms; Asset Hub solo decide seguridad/capacidad.\n"
        "- risk_reasons contiene solo riesgos o blockers reales; no incluyas razones positivas/editoriales como camara estatica, margen suficiente, no necesita acercamiento o ya esta bien encuadrado.\n"
        "- flip.allowed=true si no hay texto, logo, watermark, UI, direccionalidad semantica importante, lateralidad significativa ni otro blocker real, con confidence suficiente.\n"
        "- flip.allowed=false requiere risk_reasons concretas; no bloquees solo por postura asimetrica o manos asimetricas salvo que el flip cambie significado o introduzca inconsistencia semantica observable.\n"
        "- zoom.allowed indica seguridad, no necesidad: si un zoom pequeno conservador es seguro, allowed=true aunque el plano ya este bien encuadrado; si allowed=true, max_safe_zoom debe ser > 1.0 y nunca mayor a 1.10.\n"
        "- zoom.max_safe_zoom nunca puede ser 0; el minimo valido es 1.0. 1.0 significa identity/no safe zoom. Si zoom.allowed=false, usa max_safe_zoom=1.0. Si zoom.allowed=true, max_safe_zoom debe ser > 1.0 y <= 1.10.\n"
        "- zoom.allowed=false solo por clipping, resolucion insuficiente, sujeto demasiado cerca del borde, perdida clara de contenido o blocker visual real; no uses no necesita zoom como risk_reason.\n"
        "- pan.allowed=true si un pan suave es seguro; camara estatica y margen suficiente son evidencia favorable. safe_directions debe tener al menos una direccion y max_offset_x/y valores conservadores > 0 cuando correspondan.\n"
        "- pan.allowed=false solo por movimiento de camara significativo, movimiento de sujeto incompatible, falta de margen o riesgo real de crop/clipping; no es necesario hacer pan no es blocker.\n"
        "- crop_vertical y crop_horizontal son decisiones independientes; no infieras una desde la otra. Si allowed=true, safe_rect DEBE estar presente y ser valido. Si no puedes estimar geometria segura, allowed=false y puedes incluir geometry_unavailable.\n"
        "- Cada transforms.*.confidence es REQUIRED float 0.0..1.0, estimado independientemente segun evidencia real de los frames/contact-sheet.\n"
        "- No uses 0.0 como placeholder ni copies valores de ejemplo; 0.0 solo aplica cuando literalmente no existe evidencia util para evaluar ese transform.\n"
        "- subject_region DEBE ser null o [x,y,w,h] con coordenadas numericas normalizadas 0..1; nunca palabras como center, centro, left o right.\n"
        "- subject_trajectory DEBE ser [] o array de objetos {timestamp, center:[x,y], bbox:[x,y,w,h]}; nunca acciones o direcciones como strings.\n"
        "- safe_rect DEBE ser null o [x,y,w,h] con coordenadas numericas normalizadas 0..1.\n"
        "- Prefiere null o [] antes que geometria estimada sin confianza.\n"
        "- preferred_aspect_ratios solo puede usar 9:16, 16:9, 1:1 o 4:5.\n"
        "- safe_text_areas solo puede usar top, middle, bottom, left, right o none.\n"
        "- label debe ser excellent, good, usable, weak o bad.\n\n"
        f"Asset: id={asset.id}, filename={asset.filename}, duration={asset.duration_seconds}, "
        f"orientation={asset.orientation}, width={asset.width}, height={asset.height}."
    )


def apply_visual_intelligence_result(
    session: Session,
    asset: Asset,
    result: VisualIntelligenceResult,
    inputs: VisualFrameInputs,
    profile_version: str,
) -> None:
    decision = decide_visual_editorial_status(result)
    transforms = normalize_visual_transforms(result, asset)
    result_json = visual_result_json(
        asset,
        result,
        inputs,
        profile_version,
        status="ready",
        decision=decision,
        transforms=transforms,
    )
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=get_settings().nvidia_visual_model,
            provider="nvidia",
            input_type=VISUAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json=result_json,
            confidence=result.confidence,
        )
    )
    now = datetime.now(UTC)
    asset.quality_score = result.quality.score
    asset.editorial_status = decision.status
    asset.editorial_quality_score = result.quality.score
    asset.vertical_suitability_score = result.composition.vertical_suitability
    asset.horizontal_suitability_score = result.composition.horizontal_suitability
    asset.editorial_reason_codes = decision.reason_codes
    asset.quality_analyzed_at = now
    asset.quality_profile_version = profile_version
    asset.visual_description = result.semantics.summary_es
    asset.action_description = ", ".join(result.semantics.actions) or asset.action_description
    asset.emotion = ", ".join(result.semantics.emotions or ([result.semantics.mood] if result.semantics.mood else [])) or None
    asset.location = result.semantics.setting
    asset.best_for = ", ".join(result.semantics.possible_use_cases) or asset.best_for
    asset.avoid_for = ", ".join(result.semantics.negative_use_cases) or asset.avoid_for
    asset.has_visible_text = bool(result.semantics.visible_text)
    asset.visible_text = result.semantics.visible_text
    asset.has_logo = bool(result.garbage.logo or result.semantics.logo_or_watermark)
    asset.has_watermark = bool(result.garbage.watermark or result.semantics.logo_or_watermark)
    asset.shot_type = normalize_shot_type(result.composition.shot_type)
    asset.camera_motion = normalize_camera_motion(result.composition.camera_motion)
    asset.subject_position = normalize_subject_position(result.composition.subject_position)
    asset.has_directional_motion = has_directional_signal(result)
    asset.search_text = " ".join(
        [
            result.semantics.summary_es,
            *result.semantics.subjects,
            *result.semantics.actions,
            *result.semantics.objects,
            *result.semantics.emotions,
            *result.semantics.narrative_themes,
            *result.semantics.possible_use_cases,
            *result.semantics.negative_use_cases,
            *result.semantics.keywords_es,
        ]
    ).strip() or asset.search_text
    asset.embedding_text = asset.search_text or asset.embedding_text
    asset.flip_horizontal_allowed = transforms.flip_allowed
    asset.flip_risk_reasons = transforms.flip_risk_reasons
    asset.zoom_allowed = transforms.zoom_allowed
    asset.max_safe_zoom = transforms.max_safe_zoom
    asset.crop_allowed = transforms.crop_allowed
    asset.safe_text_areas = transforms.safe_text_areas
    asset.ai_enrichment_confidence = result.confidence
    asset.updated_at = now


def apply_failed_visual_intelligence_result(
    session: Session,
    asset: Asset,
    profile_version: str,
    exc: Exception,
) -> None:
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=get_settings().nvidia_visual_model,
            provider="nvidia",
            input_type=VISUAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json={
                "analysis_type": VISUAL_ANALYSIS_TYPE,
                "profile_version": profile_version,
                "model": get_settings().nvidia_visual_model,
                "status": "failed",
                "source_fingerprint": source_fingerprint(asset),
                "error": sanitize_provider_error(str(exc)),
                "error_type": "operational_failure",
                "retryable": getattr(
                    exc,
                    "_visual_retryable",
                    is_transient_nvidia_error(exc) if isinstance(exc, NvidiaProviderError) else False,
                ),
                "attempt_count": getattr(exc, "_visual_attempt_count", 1),
                "final_error": getattr(exc, "_visual_final_error", sanitize_provider_error(str(exc))),
            },
            confidence=None,
        )
    )


def visual_result_json(
    asset: Asset,
    result: VisualIntelligenceResult,
    inputs: VisualFrameInputs,
    profile_version: str,
    *,
    status: str,
    decision: VisualEditorialDecision | None = None,
    transforms: NormalizedVisualTransforms | None = None,
) -> dict[str, Any]:
    normalization_warnings = dedupe_codes(
        [
            *list(getattr(result, "_normalization_warnings", [])),
            *(transforms.consistency_warnings if transforms else []),
        ]
    )
    return {
        "analysis_type": VISUAL_ANALYSIS_TYPE,
        "profile_version": profile_version,
        "model": get_settings().nvidia_visual_model,
        "status": status,
        "source_fingerprint": source_fingerprint(asset),
        "frame_count": len(inputs.frame_paths),
        "frame_cache_path": str(inputs.frame_cache_dir),
        "frames": [path.name for path in inputs.frame_paths],
        "normalization_warnings": normalization_warnings,
        "attempt_count": getattr(result, "_attempt_count", 1),
        "final_error": getattr(result, "_final_error", None),
        "visual": result.model_dump(mode="json"),
        "editorial_policy": {
            "status": decision.status if decision else None,
            "reason_codes": decision.reason_codes if decision else [],
        },
        "normalized_transforms": asdict(transforms) if transforms else None,
        "error": None,
    }


def source_fingerprint(asset: Asset) -> str:
    parts = [
        asset.checksum,
        asset.source_hash,
        str(asset.source_size_bytes or asset.size_bytes or ""),
        asset.source_modified_at.isoformat() if asset.source_modified_at else "",
        asset.remote_file_id or asset.drive_file_id or "",
        asset.remote_path,
    ]
    return "|".join(str(part or "") for part in parts)
