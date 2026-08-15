from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Asset, AssetAIAnalysis
from app.schemas.editorial_quality import (
    EDITORIAL_QUALITY_PROMPT_VERSION,
    QUALITY_PROFILE_VERSION,
    EditorialQualityVLMResult,
)
from app.services.ai_asset_enrichment import AssetImageInputs, collect_asset_images
from app.services.ai_providers.nvidia_provider import extract_json_object, post_chat_completion
from app.services.ai_providers.openai_provider import sanitize_provider_error

EDITORIAL_ANALYSIS_TYPE = "editorial_quality"
HIGH_CONFIDENCE_THRESHOLD = 0.75
LOW_CONFIDENCE_THRESHOLD = 0.60
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


class EditorialQualityOperationalError(RuntimeError):
    pass


@dataclass(frozen=True)
class EditorialQualityDecision:
    status: str
    reason_codes: list[str]
    deterministic: dict[str, Any]


@dataclass(frozen=True)
class EditorialBackfillResult:
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


VisionCaller = Callable[[str, list[Path]], EditorialQualityVLMResult]


def analyze_asset_editorial_quality(
    session: Session,
    asset_id: int,
    *,
    force: bool = False,
    dry_run: bool = False,
    profile_version: str = QUALITY_PROFILE_VERSION,
    vision_caller: VisionCaller | None = None,
) -> Asset:
    asset = session.scalar(
        select(Asset).where(Asset.id == asset_id).options(selectinload(Asset.source))
    )
    if asset is None:
        raise ValueError(f"Asset not found: {asset_id}")
    if not force and asset_quality_is_current(asset, profile_version):
        return asset
    if not is_quality_candidate(asset):
        return asset
    if dry_run:
        return asset

    inputs: AssetImageInputs | None = None
    try:
        deterministic = deterministic_quality_signals(asset)
        vlm_result: EditorialQualityVLMResult | None = None
        if not deterministic.get("content_decode_impossible"):
            inputs = collect_asset_images(asset)
            if inputs.image_paths:
                caller = vision_caller or call_nvidia_editorial_quality
                vlm_result = caller(build_editorial_quality_prompt(asset, deterministic), inputs.image_paths)
            else:
                deterministic["missing_visual_sample"] = True

        decision = decide_editorial_status(deterministic, vlm_result)
        apply_editorial_quality_result(
            session,
            asset,
            decision,
            vlm_result,
            inputs.input_type if inputs is not None else "metadata",
            profile_version,
            error=None,
        )
        session.commit()
        return asset
    except Exception as exc:
        session.rollback()
        failed = session.get(Asset, asset_id)
        if failed is None:
            raise
        apply_failed_editorial_quality_result(session, failed, profile_version, exc)
        session.commit()
        return failed
    finally:
        if inputs is not None and inputs.temp_dir is not None:
            shutil.rmtree(inputs.temp_dir, ignore_errors=True)


def run_editorial_quality_backfill(
    session: Session,
    *,
    limit: int | None = None,
    batch_size: int = 20,
    apply: bool = False,
    force: bool = False,
    profile_version: str = QUALITY_PROFILE_VERSION,
    vision_caller: VisionCaller | None = None,
) -> EditorialBackfillResult:
    bounded_batch_size = max(1, min(batch_size, 200))
    selected = processed = skipped = failed = 0
    remaining_limit = limit if limit is not None else None
    seen_asset_ids: set[int] = set()

    while remaining_limit is None or remaining_limit > 0:
        current_limit = bounded_batch_size if remaining_limit is None else min(bounded_batch_size, remaining_limit)
        ids = [
            asset_id
            for asset_id in select_quality_candidate_ids(
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
                asset = analyze_asset_editorial_quality(
                    session,
                    asset_id,
                    force=force,
                    profile_version=profile_version,
                    vision_caller=vision_caller,
                )
            except KeyboardInterrupt:
                raise
            except Exception:
                session.rollback()
                failed += 1
                continue
            if before is not None and asset.quality_profile_version == profile_version:
                processed += 1
                if asset.editorial_status == "pending":
                    failed += 1
            else:
                skipped += 1
        if remaining_limit is not None:
            remaining_limit -= len(ids)

    return EditorialBackfillResult(
        dry_run=not apply,
        profile_version=profile_version,
        limit=limit,
        batch_size=bounded_batch_size,
        selected=selected,
        processed=processed,
        skipped=skipped,
        failed=failed,
        remaining=count_remaining_quality_assets(session, profile_version=profile_version, force=False),
    )


def editorial_quality_status(session: Session, *, profile_version: str = QUALITY_PROFILE_VERSION) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(Asset)) or 0
    by_status = {
        status: session.scalar(select(func.count()).select_from(Asset).where(Asset.editorial_status == status)) or 0
        for status in ("pending", "searchable", "quarantined", "rejected")
    }
    processed = (
        session.scalar(
            select(func.count()).select_from(Asset).where(Asset.quality_profile_version == profile_version)
        )
        or 0
    )
    failed = (
        session.scalar(
            select(func.count())
            .select_from(AssetAIAnalysis)
            .where(
                AssetAIAnalysis.prompt_version == profile_version,
                AssetAIAnalysis.input_type == EDITORIAL_ANALYSIS_TYPE,
                AssetAIAnalysis.result_json["error"].as_string().is_not(None),
            )
        )
        or 0
    )
    remaining = count_remaining_quality_assets(session, profile_version=profile_version, force=False)
    return {
        "total": total,
        **by_status,
        "pipeline": {
            "profile_version": profile_version,
            "processed": processed,
            "failed": failed,
            "remaining": remaining,
        },
    }


def select_quality_candidate_ids(
    session: Session,
    *,
    limit: int,
    force: bool = False,
    profile_version: str = QUALITY_PROFILE_VERSION,
) -> list[int]:
    query = (
        select(Asset.id)
        .where(quality_candidate_filter())
        .order_by(Asset.quality_analyzed_at.asc().nullsfirst(), Asset.id.asc())
        .limit(limit)
    )
    if not force:
        query = query.where(
            or_(
                Asset.quality_profile_version.is_(None),
                Asset.quality_profile_version != profile_version,
                Asset.editorial_status == "pending",
            )
        )
    return list(session.scalars(query))


def count_remaining_quality_assets(
    session: Session,
    *,
    profile_version: str = QUALITY_PROFILE_VERSION,
    force: bool = False,
) -> int:
    query = select(func.count()).select_from(Asset).where(quality_candidate_filter())
    if not force:
        query = query.where(
            or_(
                Asset.quality_profile_version.is_(None),
                Asset.quality_profile_version != profile_version,
                Asset.editorial_status == "pending",
            )
        )
    return int(session.scalar(query) or 0)


def quality_candidate_filter():
    return and_(
        Asset.status == "ready",
        Asset.move_status == "moved",
        Asset.source_status == "active",
        Asset.type.in_(("image", "video")),
    )


def is_quality_candidate(asset: Asset) -> bool:
    return (
        asset.status == "ready"
        and asset.move_status == "moved"
        and asset.source_status == "active"
        and asset.type in {"image", "video"}
    )


def asset_quality_is_current(asset: Asset, profile_version: str) -> bool:
    if asset.quality_profile_version != profile_version or asset.editorial_status == "pending":
        return False
    analysis = latest_editorial_analysis(asset)
    if analysis is None:
        return True
    result = analysis.result_json or {}
    return result.get("source_fingerprint") == source_fingerprint(asset)


def latest_editorial_analysis(asset: Asset) -> AssetAIAnalysis | None:
    analyses = [
        analysis
        for analysis in asset.ai_analyses
        if analysis.input_type == EDITORIAL_ANALYSIS_TYPE
        or analysis.prompt_version in {QUALITY_PROFILE_VERSION, EDITORIAL_QUALITY_PROMPT_VERSION}
    ]
    return max(analyses, key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC), default=None)


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


def deterministic_quality_signals(asset: Asset) -> dict[str, Any]:
    width = asset.width or 0
    height = asset.height or 0
    duration = asset.duration_seconds
    content_decode_impossible = content_decode_failure_is_deterministic(asset)
    signals = {
        "content_decode_impossible": content_decode_impossible,
        "preview_generation_failed": asset.preview_status == "failed",
        "missing_dimensions": asset.type in {"image", "video"} and (width <= 0 or height <= 0),
        "missing_duration": asset.type == "video" and (duration is None or duration <= 0),
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": duration,
        "orientation": asset.orientation,
        "preview_status": asset.preview_status,
        "technical_metadata_status": asset.technical_metadata_status,
        "probe_error": asset.probe_error,
    }
    return signals


def content_decode_failure_is_deterministic(asset: Asset) -> bool:
    if asset.technical_metadata_status != "failed" and not asset.probe_error:
        return False
    error = (asset.probe_error or "").lower()
    if any(marker in error for marker in ("timeout", "timed out", "temporary", "transient", "i/o", "io error")):
        return False
    if asset.technical_metadata_status == "failed" and "ffprobe failed" in error:
        return True
    return any(
        marker in error
        for marker in (
            "invalid data",
            "moov atom not found",
            "corrupt",
            "could not find codec",
            "decode failed",
            "end of file",
        )
    )


def decide_editorial_status(
    deterministic: dict[str, Any],
    vlm_result: EditorialQualityVLMResult | None,
) -> EditorialQualityDecision:
    reason_codes: list[str] = []
    if deterministic.get("content_decode_impossible"):
        return EditorialQualityDecision("rejected", ["content_decode_impossible"], deterministic)
    if deterministic.get("preview_generation_failed"):
        raise EditorialQualityOperationalError("preview generation failed")
    if deterministic.get("missing_visual_sample"):
        raise EditorialQualityOperationalError("missing visual sample")
    if deterministic.get("missing_dimensions"):
        reason_codes.append("missing_dimensions")
    if deterministic.get("missing_duration"):
        reason_codes.append("missing_duration")
    if reason_codes and vlm_result is None:
        return EditorialQualityDecision("quarantined", reason_codes, deterministic)
    if vlm_result is None:
        return EditorialQualityDecision("quarantined", reason_codes or ["vlm_missing"], deterministic)

    reason_codes.extend(vlm_result.reason_codes)
    hard_flags = [flag for flag in HARD_REJECT_FLAGS if getattr(vlm_result, flag)]
    quarantine_flags = [flag for flag in QUARANTINE_FLAGS if getattr(vlm_result, flag)]
    confidence = vlm_result.confidence
    if hard_flags and confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return EditorialQualityDecision("rejected", dedupe_codes([*reason_codes, *hard_flags]), deterministic)
    if not vlm_result.editorial_usable and confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return EditorialQualityDecision("rejected", dedupe_codes([*reason_codes, "not_editorial_usable"]), deterministic)
    if (
        hard_flags
        or quarantine_flags
        or confidence < LOW_CONFIDENCE_THRESHOLD
        or vlm_result.editorial_quality_score < 0.55
        or not vlm_result.editorial_usable
        or reason_codes
    ):
        codes = [*reason_codes, *hard_flags, *quarantine_flags]
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            codes.append("low_confidence")
        if vlm_result.editorial_quality_score < 0.55:
            codes.append("low_editorial_quality")
        if not vlm_result.editorial_usable:
            codes.append("not_editorial_usable")
        return EditorialQualityDecision("quarantined", dedupe_codes(codes), deterministic)
    return EditorialQualityDecision("searchable", [], deterministic)


def apply_editorial_quality_result(
    session: Session,
    asset: Asset,
    decision: EditorialQualityDecision,
    vlm_result: EditorialQualityVLMResult | None,
    input_type: str,
    profile_version: str,
    *,
    error: str | None,
) -> None:
    now = datetime.now(UTC)
    result_json: dict[str, Any] = {
        "analysis_type": EDITORIAL_ANALYSIS_TYPE,
        "quality_profile_version": profile_version,
        "source_fingerprint": source_fingerprint(asset),
        "deterministic": decision.deterministic,
        "decision": decision.status,
        "reason_codes": decision.reason_codes,
        "vlm": vlm_result.model_dump(mode="json") if vlm_result is not None else None,
        "error": error,
    }
    confidence = vlm_result.confidence if vlm_result is not None else None
    settings = get_settings()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=settings.nvidia_model,
            provider="nvidia",
            input_type=EDITORIAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json=result_json,
            confidence=confidence,
        )
    )
    asset.editorial_status = decision.status
    asset.editorial_quality_score = vlm_result.editorial_quality_score if vlm_result else None
    asset.vertical_suitability_score = vlm_result.vertical_suitability_score if vlm_result else None
    asset.horizontal_suitability_score = vlm_result.horizontal_suitability_score if vlm_result else None
    asset.editorial_reason_codes = decision.reason_codes
    asset.quality_profile_version = profile_version
    asset.quality_analyzed_at = now
    if decision.status in {"rejected", "quarantined"}:
        asset.auto_select_enabled = False


def apply_failed_editorial_quality_result(
    session: Session,
    asset: Asset,
    profile_version: str,
    exc: Exception,
) -> None:
    now = datetime.now(UTC)
    message = sanitize_provider_error(str(exc))
    settings = get_settings()
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=settings.nvidia_model,
            provider="nvidia",
            input_type=EDITORIAL_ANALYSIS_TYPE,
            prompt_version=profile_version,
            result_json={
                "analysis_type": EDITORIAL_ANALYSIS_TYPE,
                "quality_profile_version": profile_version,
                "source_fingerprint": source_fingerprint(asset),
                "deterministic": deterministic_quality_signals(asset),
                "decision": "pending",
                "reason_codes": ["operational_failure"],
                "vlm": None,
                "error": message,
                "error_type": "operational_failure",
            },
            confidence=None,
        )
    )
    asset.editorial_status = "pending"
    asset.editorial_reason_codes = ["operational_failure"]
    asset.quality_profile_version = profile_version
    asset.quality_analyzed_at = now


def call_nvidia_editorial_quality(prompt: str, image_paths: list[Path]) -> EditorialQualityVLMResult:
    settings = get_settings()
    if not settings.nvidia_api_key:
        raise RuntimeError("NVIDIA_API_KEY is not configured")
    text = post_chat_completion(prompt, image_paths, settings.nvidia_model, timeout_seconds=60.0)
    try:
        payload = json.loads(extract_json_object(text))
    except Exception as exc:
        raise RuntimeError("NVIDIA returned invalid editorial quality JSON") from exc
    return EditorialQualityVLMResult.model_validate(payload)


def build_editorial_quality_prompt(asset: Asset, deterministic: dict[str, Any]) -> str:
    return (
        "Evalua estos frames de un asset visual para decidir si puede competir como stock editorial.\n"
        "Responde exclusivamente con un objeto JSON valido, sin markdown ni explicaciones.\n"
        "Usa exactamente estas claves y tipos:\n"
        "{\n"
        '  "editorial_quality_score": 0.0,\n'
        '  "vertical_suitability_score": 0.0,\n'
        '  "horizontal_suitability_score": 0.0,\n'
        '  "subject_severely_out_of_frame": false,\n'
        '  "subject_badly_clipped": false,\n'
        '  "social_media_ui": false,\n'
        '  "subscribe_cta": false,\n'
        '  "emoji_overlay": false,\n'
        '  "watermark": false,\n'
        '  "heavy_text_overlay": false,\n'
        '  "nearly_empty": false,\n'
        '  "severe_blur": false,\n'
        '  "severe_black_frames": false,\n'
        '  "editorial_usable": true,\n'
        '  "confidence": 0.0,\n'
        '  "reason_codes": []\n'
        "}\n"
        "Marca hard issues solo cuando sean claros: sujeto severamente fuera de cuadro, recorte inutilizable, UI social dominante, "
        "CTA subscribe incrustado, emojis/stickers problematicos, frames negros/vacios, blur catastrofico o stock claramente inutilizable. "
        "Watermarks, texto considerable, framing mediocre o dudas deben bajar score y aparecer en reason_codes.\n"
        f"Metadatos tecnicos conocidos: {json.dumps(deterministic, ensure_ascii=False, default=str)}\n"
        f"Asset: id={asset.id}, uid={asset.asset_uid}, filename={asset.filename}, type={asset.type}, orientation={asset.orientation}."
    )


def dedupe_codes(codes: list[str]) -> list[str]:
    deduped: list[str] = []
    for raw in codes:
        code = str(raw or "").strip().lower().replace(" ", "_")
        if code and code not in deduped:
            deduped.append(code[:80])
    return deduped
