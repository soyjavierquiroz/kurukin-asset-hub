from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Asset, AssetAIAnalysis, AssetKeyword
from app.schemas.ai_enrichment import AIAssetEnrichmentResult
from app.services.ai_prompts import ASSET_ENRICHMENT_PROMPT_VERSION, build_asset_enrichment_prompt
from app.services.ai_providers.openai_provider import (
    DEFAULT_OPENAI_MODEL,
    call_openai_vision as openai_call_vision,
    sanitize_provider_error,
)
from app.services.asset_preview import safe_asset_uid

AI_TEMP_ROOT = Path("/tmp/kurukin-asset-hub-ai")


@dataclass(frozen=True)
class AssetImageInputs:
    image_paths: list[Path]
    input_type: str
    temp_dir: Path | None = None


def enrich_asset_with_ai(
    session: Session,
    asset_id: int,
    force: bool = False,
    dry_run: bool = False,
) -> Asset:
    asset = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.keywords), selectinload(Asset.source))
    )
    if asset is None:
        raise ValueError(f"Asset not found: {asset_id}")

    settings = get_settings()
    if dry_run:
        return asset
    if not settings.ai_enrichment_enabled:
        return mark_skipped(session, asset, "AI enrichment disabled")
    if asset.ai_enrichment_status == "ready" and not force:
        return asset
    if asset.type == "audio":
        return mark_skipped(session, asset, "Audio AI enrichment not implemented yet")
    if not asset.thumbnail_path and not asset.preview_path:
        return mark_skipped(session, asset, "Missing preview or thumbnail")

    inputs = collect_asset_images(asset)
    try:
        if not inputs.image_paths:
            return mark_skipped(session, asset, "Missing preview or thumbnail")

        asset.ai_enrichment_status = "processing"
        asset.ai_error = None
        asset.review_reason = None
        session.commit()

        prompt = build_ai_prompt(asset)
        result = call_openai_vision(prompt, inputs.image_paths)
        apply_ai_enrichment(session, asset, result, input_type=inputs.input_type, force=force)
        session.commit()
        return asset
    except Exception as exc:
        session.rollback()
        failed_asset = session.get(Asset, asset_id)
        if failed_asset is None:
            raise
        failed_asset.ai_enrichment_status = "failed"
        failed_asset.ai_error = sanitize_ai_error(str(exc))
        session.commit()
        return failed_asset
    finally:
        if inputs.temp_dir is not None:
            shutil.rmtree(inputs.temp_dir, ignore_errors=True)


def enrich_pending_assets(session: Session, limit: int = 20, force: bool = False) -> list[Asset]:
    settings = get_settings()
    bounded_limit = max(1, min(limit, settings.ai_max_assets_per_batch))
    query = select(Asset.id).order_by(Asset.created_at.asc(), Asset.id.asc()).limit(bounded_limit)
    if not force:
        query = query.where(Asset.ai_enrichment_status == "pending")
    asset_ids = list(session.scalars(query))

    enriched: list[Asset] = []
    for asset_id in asset_ids:
        try:
            enriched.append(enrich_asset_with_ai(session, asset_id, force=force))
        except KeyboardInterrupt:
            raise
        except Exception:
            session.rollback()
            failed_asset = session.get(Asset, asset_id)
            if failed_asset is not None:
                failed_asset.ai_enrichment_status = "failed"
                failed_asset.ai_error = "AI enrichment failed"
                session.commit()
                enriched.append(failed_asset)
    return enriched


def build_ai_prompt(asset: Asset) -> str:
    return build_asset_enrichment_prompt(asset)


def collect_asset_images(asset: Asset) -> AssetImageInputs:
    image_paths: list[Path] = []
    temp_dir: Path | None = None

    thumbnail = local_preview_file(asset.thumbnail_path)
    preview = local_preview_file(asset.preview_path)
    if thumbnail is not None and thumbnail.is_file():
        image_paths.append(thumbnail)

    if asset.type == "image":
        if preview is not None and preview.is_file() and preview not in image_paths:
            image_paths.insert(0, preview)
        return AssetImageInputs(image_paths=image_paths, input_type="preview")

    if asset.type == "video" and preview is not None and preview.is_file():
        temp_dir = AI_TEMP_ROOT / safe_asset_uid(asset.asset_uid)
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        image_paths.extend(extract_video_frames(preview, temp_dir, get_settings().ai_frame_sample_count))
        input_type = "combined" if len(image_paths) > 1 else "thumbnail"
        return AssetImageInputs(image_paths=image_paths, input_type=input_type, temp_dir=temp_dir)

    input_type = "thumbnail" if image_paths else "metadata"
    return AssetImageInputs(image_paths=image_paths, input_type=input_type, temp_dir=temp_dir)


def call_openai_vision(prompt: str, image_paths: list[Path]) -> AIAssetEnrichmentResult:
    settings = get_settings()
    return openai_call_vision(prompt, image_paths, model=settings.ai_model or DEFAULT_OPENAI_MODEL)


def apply_ai_enrichment(
    session: Session,
    asset: Asset,
    result: AIAssetEnrichmentResult,
    input_type: str = "combined",
    force: bool = False,
) -> None:
    now = datetime.now(UTC)
    settings = get_settings()
    model = settings.ai_model or DEFAULT_OPENAI_MODEL
    confidence = result.confidence
    needs_review = result.needs_human_review or confidence < settings.ai_review_threshold
    review_reason = result.review_reason
    if confidence < settings.ai_review_threshold:
        review_reason = review_reason or "AI confidence below review threshold"

    session.add(
        AssetAIAnalysis(
            asset=asset,
            model=model,
            provider=settings.ai_provider,
            input_type=input_type,
            prompt_version=ASSET_ENRICHMENT_PROMPT_VERSION,
            result_json=result.model_dump(mode="json"),
            confidence=confidence,
        )
    )

    if force or not text_or_none(asset.title):
        asset.title = trunc(result.title, 500)
    asset.visual_description = text_or_none(result.visual_description)
    asset.action_description = text_or_none(result.action_description)
    asset.emotion = trunc(result.emotion, 160)
    asset.people = trunc(result.people, 500)
    asset.location = trunc(result.location, 300)
    asset.best_for = text_or_none(result.best_for)
    asset.avoid_for = text_or_none(result.avoid_for)
    asset.negative_keywords = text_or_none(result.negative_keywords)
    asset.shot_type = result.shot_type
    asset.camera_motion = result.camera_motion
    asset.subject_position = result.subject_position
    asset.visual_energy = result.visual_energy
    asset.pacing = result.pacing
    asset.best_scene_role = result.best_scene_role
    asset.has_visible_text = result.has_visible_text
    asset.visible_text = text_or_none(result.visible_text)
    asset.text_language = trunc(result.text_language, 40)
    asset.has_logo = result.has_logo
    asset.has_watermark = result.has_watermark
    asset.has_faces = result.has_faces
    asset.has_hands = result.has_hands
    asset.has_product = result.has_product
    asset.has_directional_motion = result.has_directional_motion
    asset.safe_for_subtitles = result.safe_for_subtitles
    asset.safe_for_text_overlay = result.safe_for_text_overlay
    asset.overlay_safe_area = result.overlay_safe_area
    asset.flip_horizontal_allowed = result.flip_horizontal_allowed
    asset.flip_vertical_allowed = result.flip_vertical_allowed
    asset.crop_allowed = result.crop_allowed
    asset.zoom_allowed = result.zoom_allowed
    asset.speed_change_allowed = result.speed_change_allowed
    asset.reverse_allowed = result.reverse_allowed
    asset.color_grade_allowed = result.color_grade_allowed
    asset.loopable = result.loopable
    asset.similarity_group = trunc(result.similarity_group, 160)
    asset.search_text = result.search_text.strip()
    asset.embedding_text = result.embedding_text.strip()
    asset.ai_enrichment_status = "needs_review" if needs_review else "ready"
    asset.ai_enrichment_confidence = confidence
    asset.ai_model = model
    asset.ai_enriched_at = now
    asset.ai_error = None
    asset.needs_human_review = needs_review
    asset.review_reason = text_or_none(review_reason)
    asset.enrichment_version = ASSET_ENRICHMENT_PROMPT_VERSION
    asset.auto_keywords_generated_at = now

    upsert_ai_keywords(session, asset, result, force=force)


def upsert_ai_keywords(
    session: Session,
    asset: Asset,
    result: AIAssetEnrichmentResult,
    force: bool = False,
) -> None:
    if force:
        session.execute(
            delete(AssetKeyword).where(
                AssetKeyword.asset_id == asset.id,
                AssetKeyword.source == "ai",
            )
        )
        session.flush()

    existing = session.scalars(select(AssetKeyword).where(AssetKeyword.asset_id == asset.id)).all()
    by_key = {
        (keyword.keyword, keyword.category, keyword.language): keyword
        for keyword in existing
        if keyword.source == "ai"
    }
    manual_keys = {
        (keyword.keyword, keyword.category, keyword.language)
        for keyword in existing
        if keyword.source != "ai"
    }
    default_language = get_settings().ai_output_language

    for item in result.keywords:
        keyword = normalize_keyword(item.keyword)
        if not keyword:
            continue
        language = normalize_language(item.language, default_language=default_language)
        key = (keyword, item.category, language)
        if key in manual_keys:
            continue
        existing_keyword = by_key.get(key)
        if existing_keyword is None:
            session.add(
                AssetKeyword(
                    asset=asset,
                    keyword=keyword,
                    category=item.category,
                    weight=item.weight,
                    confidence=item.confidence,
                    source="ai",
                    language=language,
                )
            )
        else:
            existing_keyword.weight = item.weight
            existing_keyword.confidence = item.confidence


def mark_skipped(session: Session, asset: Asset, reason: str) -> Asset:
    asset.ai_enrichment_status = "skipped"
    asset.needs_human_review = False
    asset.review_reason = reason
    asset.ai_error = None
    session.commit()
    return asset


def local_preview_file(relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    parts = PurePosixPath(relative_path).parts
    if not parts:
        return None
    if parts[0] == "previews":
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return Path(get_settings().preview_storage_dir).joinpath(*parts)


def extract_video_frames(preview_path: Path, temp_dir: Path, frame_count: int) -> list[Path]:
    frame_count = max(1, frame_count)
    output_pattern = temp_dir / "frame_%03d.jpg"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(preview_path),
        "-vf",
        f"fps={frame_count}/30",
        "-frames:v",
        str(frame_count),
        str(output_pattern),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return sorted(temp_dir.glob("frame_*.jpg"))[:frame_count]


def text_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def trunc(value: str | None, max_length: int) -> str | None:
    cleaned = text_or_none(value)
    if cleaned is None:
        return None
    return cleaned[:max_length]


def normalize_keyword(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip().lower()
    return cleaned[:160]


def normalize_language(value: str | None, default_language: str = "und") -> str:
    cleaned_default = (default_language or "und").strip().lower() or "und"
    cleaned = (value or cleaned_default).strip().lower()
    return cleaned[:16] or cleaned_default[:16] or "und"


def sanitize_ai_error(message: str) -> str:
    return sanitize_provider_error(message)
