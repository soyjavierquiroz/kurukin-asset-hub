from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from app.services.ai_providers.nvidia_provider import extract_json_object, post_chat_completion
from app.services.ai_providers.openai_provider import sanitize_provider_error
from app.services.asset_preview import parse_ffprobe_metadata, run_ffprobe, safe_asset_uid
from app.services.rclone_service import RcloneService

VISUAL_TEMP_ROOT = Path("/tmp/kurukin-asset-hub-visual-intelligence")
VISUAL_TERMINAL_STATUSES = ("ready", "failed")


class VisualIntelligenceOperationalError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualFrameInputs:
    master_path: Path
    frame_paths: list[Path]
    frame_cache_dir: Path
    temp_dir: Path


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

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


VisualCaller = Callable[[str, list[Path]], VisualIntelligenceResult]


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
        result = caller(build_visual_intelligence_prompt(asset), inputs.frame_paths)
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
                profile_version=profile_version,
            )
            if asset_id not in seen_asset_ids
        ][:current_limit]
        if not ids:
            break
        seen_asset_ids.update(ids)
        selected += len(ids)
        if not apply:
            skipped += len(ids)
            break
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
        if remaining_limit is not None:
            remaining_limit -= len(ids)

    return VisualBackfillResult(
        dry_run=not apply,
        profile_version=profile_version,
        limit=limit,
        batch_size=bounded_batch_size,
        selected=selected,
        processed=processed,
        skipped=skipped,
        failed=failed,
        remaining=count_remaining_visual_assets(session, profile_version=profile_version, force=False),
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
    profile_version: str = VISUAL_PROFILE_VERSION,
) -> list[int]:
    query = select(Asset.id).where(visual_candidate_filter()).order_by(Asset.id.asc()).limit(limit)
    if not force:
        latest_analysis_ids = latest_visual_analysis_ids(profile_version)
        current_assets = (
            select(AssetAIAnalysis.asset_id)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(
                AssetAIAnalysis.prompt_version == profile_version,
                AssetAIAnalysis.input_type == VISUAL_ANALYSIS_TYPE,
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
) -> int:
    query = select(func.count()).select_from(Asset).where(visual_candidate_filter())
    if not force:
        latest_analysis_ids = latest_visual_analysis_ids(profile_version)
        current_assets = (
            select(AssetAIAnalysis.asset_id)
            .join(latest_analysis_ids, latest_analysis_ids.c.analysis_id == AssetAIAnalysis.id)
            .where(
                AssetAIAnalysis.prompt_version == profile_version,
                AssetAIAnalysis.input_type == VISUAL_ANALYSIS_TYPE,
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
    return result.get("status") == "ready" and result.get("source_fingerprint") == source_fingerprint(asset)


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
    temp_dir.mkdir(parents=True, exist_ok=True)
    master_path = temp_dir / safe_asset_uid(asset.filename or f"asset-{asset.id}.mp4")
    remote = asset.rclone_remote or (asset.source.rclone_remote if asset.source else None) or get_settings().rclone_remote
    remote_path = asset.remote_path or asset.source_path
    if not remote or not remote_path:
        raise VisualIntelligenceOperationalError("asset master remote path is missing")
    RcloneService().copyto(remote, remote_path, str(master_path))
    if not master_path.is_file() or master_path.stat().st_size <= 0:
        raise VisualIntelligenceOperationalError("asset master download failed")

    frame_cache_dir = visual_frame_cache_dir(asset.id, profile_version)
    shutil.rmtree(frame_cache_dir, ignore_errors=True)
    frame_cache_dir.mkdir(parents=True, exist_ok=True)
    frames = extract_temporal_frames(master_path, frame_cache_dir, asset.duration_seconds, VISUAL_FRAME_COUNT)
    if len(frames) < 8:
        raise VisualIntelligenceOperationalError("visual intelligence requires at least 8 temporal frames")
    return VisualFrameInputs(
        master_path=master_path,
        frame_paths=frames,
        frame_cache_dir=frame_cache_dir,
        temp_dir=temp_dir,
    )


def visual_frame_cache_dir(asset_id: int, profile_version: str = VISUAL_PROFILE_VERSION) -> Path:
    return Path(get_settings().pilot_preview_root) / str(asset_id) / f"{profile_version}-frames"


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
    text = post_chat_completion(prompt, image_paths, settings.nvidia_model, timeout_seconds=90.0)
    try:
        payload = json.loads(extract_json_object(text))
    except Exception as exc:
        raise VisualIntelligenceOperationalError("NVIDIA returned invalid visual-v1 JSON") from exc
    return VisualIntelligenceResult.model_validate(payload)


def build_visual_intelligence_prompt(asset: Asset) -> str:
    return (
        "Analiza 8 a 12 frames temporales de un master de video para una biblioteca editorial.\n"
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
        '    "is_garbage": false,\n'
        '    "score": 0.0,\n'
        '    "black_or_blank": false,\n'
        '    "severe_blur": false,\n'
        '    "corrupted_frames": false,\n'
        '    "accidental_capture": false,\n'
        '    "reasons": []\n'
        "  },\n"
        '  "semantics": {\n'
        '    "summary_es": "",\n'
        '    "subjects": [],\n'
        '    "actions": [],\n'
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
        '    "vertical_suitability": 0.0,\n'
        '    "horizontal_suitability": 0.0,\n'
        '    "safe_text_areas": [],\n'
        '    "crop_risk_reasons": []\n'
        "  },\n"
        '  "transforms": {\n'
        '    "flip": {"allowed": false, "risk_reasons": []},\n'
        '    "zoom": {"allowed": false, "max_safe_zoom": 1.0, "risk_reasons": []},\n'
        '    "pan": {"allowed": false, "safe_directions": [], "risk_reasons": []},\n'
        '    "crop": {"allowed": false, "preferred_aspect_ratios": [], "risk_reasons": []}\n'
        "  },\n"
        '  "confidence": 0.0,\n'
        '  "needs_human_review": false,\n'
        '  "review_reason": null\n'
        "}\n\n"
        "Reglas de calculo:\n"
        "- quality.score combina nitidez, exposicion, estabilidad, iluminacion y consistencia temporal.\n"
        "- garbage.score mide probabilidad de asset inutil: negro, corrupto, borroso extremo o captura accidental.\n"
        "- semantics resume sujeto, accion, entorno, mood, texto visible, logos y keywords buscables.\n"
        "- composition evalua encuadre, posicion del sujeto, suitability vertical/horizontal y zonas seguras de texto.\n"
        "- flip.allowed=false si hay texto, logos, direccionalidad clara, manos asimetricas o señales culturales.\n"
        "- zoom.allowed=false si el sujeto ya esta recortado, hay texto importante o poca resolucion visual.\n"
        "- pan.allowed=true solo cuando hay margen compositivo; safe_directions indica direcciones sin cortar sujeto.\n"
        "- crop.allowed=true solo si se puede recortar sin perder sujeto, producto, texto relevante o contexto esencial.\n"
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
    result_json = visual_result_json(asset, result, inputs, profile_version, status="ready")
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=get_settings().nvidia_model,
            provider="nvidia",
            input_type=VISUAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json=result_json,
            confidence=result.confidence,
        )
    )
    asset.quality_score = result.quality.score
    asset.visual_description = result.semantics.summary_es
    asset.search_text = " ".join(
        [
            result.semantics.summary_es,
            *result.semantics.subjects,
            *result.semantics.actions,
            *result.semantics.keywords_es,
        ]
    ).strip() or asset.search_text
    asset.embedding_text = asset.search_text or asset.embedding_text
    asset.flip_horizontal_allowed = result.transforms.flip.allowed
    asset.flip_risk_reasons = result.transforms.flip.risk_reasons
    asset.zoom_allowed = result.transforms.zoom.allowed
    asset.max_safe_zoom = result.transforms.zoom.max_safe_zoom
    asset.crop_allowed = result.transforms.crop.allowed
    asset.safe_text_areas = result.composition.safe_text_areas
    asset.ai_enrichment_confidence = result.confidence
    asset.enrichment_version = profile_version
    asset.updated_at = datetime.now(UTC)


def apply_failed_visual_intelligence_result(
    session: Session,
    asset: Asset,
    profile_version: str,
    exc: Exception,
) -> None:
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=get_settings().nvidia_model,
            provider="nvidia",
            input_type=VISUAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json={
                "analysis_type": VISUAL_ANALYSIS_TYPE,
                "profile_version": profile_version,
                "status": "failed",
                "source_fingerprint": source_fingerprint(asset),
                "error": sanitize_provider_error(str(exc)),
                "error_type": "operational_failure",
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
) -> dict[str, Any]:
    return {
        "analysis_type": VISUAL_ANALYSIS_TYPE,
        "profile_version": profile_version,
        "status": status,
        "source_fingerprint": source_fingerprint(asset),
        "frame_count": len(inputs.frame_paths),
        "frame_cache_path": str(inputs.frame_cache_dir),
        "frames": [path.name for path in inputs.frame_paths],
        "visual": result.model_dump(mode="json"),
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
