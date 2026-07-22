from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Asset, AssetKeyword, AssetSegment, AssetSegmentationRun, Source
from app.services.ai_asset_enrichment import enrich_asset_with_ai
from app.services.asset_preview import generate_asset_preview, run_ffprobe, sanitize_error_message
from app.services.ai_providers.openai_provider import DEFAULT_OPENAI_MODEL, image_to_data_url, sanitize_provider_error
from app.services.rclone_service import RcloneService, sanitize_rclone_message

CATEGORY_FALLBACK = "other"
PERSON_SEGMENT_CATEGORIES = {"women", "men", "couples", "family", "business", "health", "spiritual", "other"}
logger = logging.getLogger(__name__)


class LongVideoSegmentationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass(frozen=True)
class SegmentNaming:
    category: str
    title: str
    filename_slug: str
    keywords: list[str]
    confidence: float
    needs_review: bool


class SegmentNamingPayload(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    filename_slug: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=40)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool


def segment_long_video_asset(
    session: Session,
    asset_id: int,
    force: bool = False,
    created_by: str | None = None,
    derived_remote: str | None = None,
    derived_root: str | None = None,
    skip_ai: bool = False,
    raw_video_id: int | None = None,
) -> AssetSegmentationRun:
    settings = get_settings()
    if not settings.long_video_segmentation_enabled:
        raise LongVideoSegmentationError("Long video segmentation is disabled")

    parent = session.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.source), selectinload(Asset.niches))
    )
    if parent is None:
        raise LongVideoSegmentationError(f"Asset not found: {asset_id}")
    if parent.type != "video":
        raise LongVideoSegmentationError("Asset is not a video")
    if parent.is_derivative and not force:
        raise LongVideoSegmentationError("Derivative assets are not segmented")

    if not force and (parent.segmentation_status == "ready" or source_video_already_processed(session, parent.remote_path)):
        return skip_already_processed_video(session, parent, created_by=created_by)
    if parent.segmentation_status == "processing" and not force:
        raise LongVideoSegmentationError("Asset is already processing")

    logger.info("PROCESS %s", parent.remote_path)

    now = datetime.now(UTC)
    config = segmentation_config()
    run = AssetSegmentationRun(
        run_uid=f"seg-{uuid4().hex}",
        parent_asset=parent,
        status="processing",
        started_at=now,
        config_json=config,
        created_by=created_by,
    )
    session.add(run)
    parent.segmentation_status = "processing"
    parent.auto_select_enabled = False
    session.commit()

    duration = parent.duration_seconds
    if duration is not None and duration <= settings.long_video_threshold_seconds:
        parent.segmentation_status = "skipped"
        parent.segment_count = 0
        parent.segmented_at = datetime.now(UTC)
        parent.auto_select_enabled = False
        run.status = "ready"
        run.completed_at = datetime.now(UTC)
        run.report_json = {"skipped": True, "reason": "below_threshold", "children_created": 0}
        session.commit()
        return run

    remote = clean_text(derived_remote) or clean_text(parent.source.derived_rclone_remote if parent.source else None)
    root = resolve_derived_root(derived_root, parent.source if parent.source else None)
    if not remote:
        fail_run(session, parent, run, "Missing derived rclone remote")
        return run
    try:
        validate_rclone_remote_exists(remote, role="derived")
    except LongVideoSegmentationError as exc:
        fail_run(session, parent, run, str(exc))
        return run

    created_count = 0
    failed_count = 0
    segment_count = 0
    uploaded_remote_paths: list[str] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="asset-hub-segments-"))
    preserve_temp_dir = False
    try:
        input_path = temp_dir / safe_filename(parent.filename)
        RcloneService().copyto(parent.rclone_remote or "", parent.remote_path, str(input_path))
        plans = detect_video_segments(input_path, config)
        if not plans:
            raise LongVideoSegmentationError("No useful segments detected")
        segment_count = len(plans)
        derived_source = get_or_create_derived_source(session, parent, remote, root)

        for plan in plans:
            segment_row = AssetSegment(
                run=run,
                parent_asset_id=parent.id,
                segment_index=plan.index,
                start_seconds=plan.start_seconds,
                end_seconds=plan.end_seconds,
                duration_seconds=plan.duration_seconds,
                status="planned",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(segment_row)
            session.commit()
            try:
                fallback_naming = suggest_segment_title_and_filename(parent, plan)
                temp_output_path = temp_dir / f"segment_{plan.index:03d}_analysis.mp4"
                export_segment(input_path, temp_output_path, plan.start_seconds, plan.end_seconds, config)
                thumbnail_path = extract_segment_thumbnail(temp_output_path, temp_dir, plan.index)
                naming = safe_analyze_segment_for_naming(
                    temp_output_path,
                    thumbnail_path,
                    parent,
                    plan,
                    fallback_naming,
                    skip_ai=skip_ai,
                )
                category = classify_segment_category(naming.category)
                output_filename = safe_segment_filename(naming.filename_slug, plan.index)
                output_path = temp_dir / output_filename
                if temp_output_path != output_path and temp_output_path.exists():
                    temp_output_path.replace(output_path)
                elif temp_output_path != output_path and not output_path.exists():
                    output_path = temp_output_path
                segment_row.status = "created"
                segment_row.category = category
                segment_row.title = naming.title
                segment_row.output_filename = output_filename
                segment_row.updated_at = datetime.now(UTC)
                session.commit()

                remote_path = build_derived_remote_path(root, category, output_filename)
                upload_segment_to_drive(output_path, remote, remote_path)
                uploaded_remote_paths.append(remote_path)
                segment_row.status = "uploaded"
                segment_row.remote_path = remote_path
                segment_row.updated_at = datetime.now(UTC)
                session.commit()

                child = create_derived_asset(
                    session=session,
                    parent=parent,
                    source=derived_source,
                    plan=plan,
                    title=naming.title,
                    output_filename=output_filename,
                    category=category,
                    remote=remote,
                    remote_path=remote_path,
                    naming=naming,
                    raw_video_id=raw_video_id,
                )
                segment_row.child_asset = child
                segment_row.status = "cataloged"
                segment_row.updated_at = datetime.now(UTC)
                session.commit()
                logger.info("GENERATED asset=%s", child.id)
                generate_child_enrichment(session, child.id, skip_ai=skip_ai)
                created_count += 1
            except Exception as exc:
                session.rollback()
                segment_row = session.get(AssetSegment, segment_row.id)
                if segment_row is not None:
                    segment_row.status = "failed"
                    segment_row.error = sanitize_segmentation_error(str(exc))
                    segment_row.updated_at = datetime.now(UTC)
                    session.commit()
                failed_count += 1

        preserve_temp_dir = created_count == 0 and failed_count > 0
        mark_parent_segmented(session, parent.id, segment_count, created_count, failed_count)
        run.status = "ready" if created_count and not failed_count else "partial" if created_count else "failed"
        run.completed_at = datetime.now(UTC)
        run.report_json = {
            "segments_planned": segment_count,
            "children_created": created_count,
            "failed": failed_count,
            "derived_remote": remote,
            "derived_root": root,
            "remote_paths": uploaded_remote_paths,
        }
        if preserve_temp_dir:
            run.report_json["temp_dir"] = str(temp_dir)
            run.error = "Segmentation failed; temporary files preserved"
        session.commit()
        logger.info("FINISHED")
        return run
    except Exception as exc:
        session.rollback()
        preserve_temp_dir = True
        fresh_parent = session.get(Asset, parent.id)
        fresh_run = session.get(AssetSegmentationRun, run.id)
        if fresh_parent is not None and fresh_run is not None:
            fail_run(
                session,
                fresh_parent,
                fresh_run,
                str(exc),
                extra_report={"temp_dir": str(temp_dir)},
            )
            return fresh_run
        raise
    finally:
        if not preserve_temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            logger.warning("PRESERVED_TEMP_DIR %s", temp_dir)


def segment_long_videos_for_source(
    session: Session,
    source_id: int | str,
    limit: int = 10,
    force: bool = False,
    created_by: str | None = None,
    derived_remote: str | None = None,
    derived_root: str | None = None,
    skip_ai: bool = False,
) -> list[AssetSegmentationRun]:
    source_filter = Source.id == source_id if isinstance(source_id, int) else Source.source_id == source_id
    source = session.scalar(select(Source).where(source_filter))
    if source is None:
        raise LongVideoSegmentationError(f"Source not found: {source_id}")
    settings = get_settings()
    query = (
        select(Asset.id)
        .where(
            Asset.source_id == source.id,
            Asset.type == "video",
            Asset.is_derivative.is_(False),
            Asset.source_status == "active",
        )
        .order_by(Asset.created_at.asc(), Asset.id.asc())
        .limit(max(1, limit))
    )
    if not force:
        query = query.where(
            Asset.duration_seconds > settings.long_video_threshold_seconds,
        )
    runs = []
    for asset_id in session.scalars(query).all():
        runs.append(
            segment_long_video_asset(
                session,
                asset_id,
                force=force,
                created_by=created_by,
                derived_remote=derived_remote,
                derived_root=derived_root,
                skip_ai=skip_ai,
            )
        )
    return runs


def source_video_already_processed(session: Session, source_video: str) -> bool:
    return session.scalar(select(func.count(Asset.id)).where(Asset.source_video == source_video)) > 0


def skip_already_processed_video(
    session: Session,
    parent: Asset,
    created_by: str | None = None,
) -> AssetSegmentationRun:
    logger.info("SKIP %s (already processed)", parent.remote_path)
    parent.segmentation_status = "ready"
    parent.auto_select_enabled = False
    run = AssetSegmentationRun(
        run_uid=f"seg-{uuid4().hex}",
        parent_asset=parent,
        status="ready",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        config_json=segmentation_config(),
        report_json={"skipped": True, "reason": "already_processed", "children_created": 0},
        created_by=created_by,
    )
    session.add(run)
    session.commit()
    return run


def detect_video_segments(input_path: Path, config: dict[str, object]) -> list[SegmentPlan]:
    duration = probe_duration(input_path)
    if duration <= 0:
        return []
    plans = detect_scene_segments(input_path, duration, config)
    if not plans:
        plans = fallback_window_segments(duration, config)
    return renumber_plans(plans)


def export_segment(input_path: Path, output_path: Path, start: float, end: float, config: dict[str, object]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_export_segment_command(input_path, output_path, start, end, config)
    run_subprocess(command, "ffmpeg segment export", timeout=1800)


def build_export_segment_command(
    input_path: Path,
    output_path: Path,
    start: float,
    end: float,
    config: dict[str, object],
) -> list[str]:
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if bool(config.get("strip_audio", True)):
        command.append("-an")

    if str(config.get("output_mode") or "high_quality_encode") == "stream_copy":
        command.extend(["-c:v", "copy", str(output_path)])
        return command

    command.extend(
        [
            "-c:v",
            "libx264",
            "-crf",
            str(config["output_crf"]),
            "-preset",
            str(config["output_preset"]),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def extract_segment_thumbnail(local_clip_path: Path, temp_dir: Path, index: int) -> Path | None:
    if not local_clip_path.exists():
        return None
    thumbnail_path = temp_dir / f"segment_{index:03d}_thumbnail.jpg"
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        "0.500",
        "-i",
        str(local_clip_path),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(720,iw)':-2",
        str(thumbnail_path),
    ]
    try:
        run_subprocess(command, "ffmpeg segment thumbnail", timeout=120)
    except LongVideoSegmentationError:
        return None
    return thumbnail_path if thumbnail_path.is_file() else None


def classify_segment_category(category: str | None) -> str:
    allowed = allowed_categories()
    cleaned = (category or CATEGORY_FALLBACK).strip().lower()
    if cleaned not in PERSON_SEGMENT_CATEGORIES:
        return CATEGORY_FALLBACK
    return cleaned if cleaned in allowed else CATEGORY_FALLBACK


def suggest_segment_title_and_filename(parent_asset: Asset, plan: SegmentPlan) -> SegmentNaming:
    base = parent_asset.title or Path(parent_asset.filename).stem
    slug = slugify_es(base)
    return SegmentNaming(
        category=CATEGORY_FALLBACK,
        title=f"{base} segmento {plan.index:03d}",
        filename_slug=f"{slug}_segment_{plan.index:03d}",
        keywords=[],
        confidence=0.0,
        needs_review=True,
    )


def analyze_segment_for_naming(
    local_clip_path: Path,
    local_thumbnail_path: Path | None,
    parent_asset: Asset,
    plan: SegmentPlan | None = None,
) -> SegmentNaming:
    segment_plan = plan or SegmentPlan(1, 0, 0)
    fallback = suggest_segment_title_and_filename(parent_asset, segment_plan)
    settings = get_settings()
    if not settings.openai_api_key:
        return fallback

    prompt = build_segment_naming_prompt(parent_asset, segment_plan)
    image_paths = [local_thumbnail_path] if local_thumbnail_path and local_thumbnail_path.is_file() else []
    if not image_paths:
        return fallback
    try:
        payload = call_openai_segment_naming(prompt, image_paths)
        category = classify_segment_category(payload.category)
        if category == CATEGORY_FALLBACK and payload.category.strip().lower() != CATEGORY_FALLBACK:
            return fallback
        slug = safe_slug(payload.filename_slug) or fallback.filename_slug
        title = clean_text(payload.title) or fallback.title
        keywords = [keyword for keyword in (safe_keyword(item) for item in payload.keywords) if keyword]
        confidence = max(0.0, min(1.0, payload.confidence))
        needs_review = bool(payload.needs_review) or confidence < settings.ai_review_threshold
        if needs_review and confidence < settings.ai_review_threshold:
            category = CATEGORY_FALLBACK
        return SegmentNaming(
            category=category,
            title=title[:120],
            filename_slug=slug,
            keywords=keywords[:12],
            confidence=confidence,
            needs_review=needs_review,
        )
    except Exception:
        return fallback


def safe_analyze_segment_for_naming(
    local_clip_path: Path,
    local_thumbnail_path: Path | None,
    parent_asset: Asset,
    plan: SegmentPlan,
    fallback: SegmentNaming,
    skip_ai: bool = False,
) -> SegmentNaming:
    if skip_ai:
        return fallback
    try:
        naming = analyze_segment_for_naming(local_clip_path, local_thumbnail_path, parent_asset, plan)
    except Exception:
        return fallback
    category = classify_segment_category(naming.category)
    if category == CATEGORY_FALLBACK and naming.category.strip().lower() != CATEGORY_FALLBACK:
        return fallback
    slug = safe_slug(naming.filename_slug) or fallback.filename_slug
    return SegmentNaming(
        category=category,
        title=(clean_text(naming.title) or fallback.title)[:120],
        filename_slug=slug,
        keywords=[keyword for keyword in (safe_keyword(item) for item in naming.keywords) if keyword][:12],
        confidence=max(0.0, min(1.0, naming.confidence)),
        needs_review=bool(naming.needs_review),
    )


def build_segment_naming_prompt(parent_asset: Asset, plan: SegmentPlan) -> str:
    allowed = ", ".join(sorted(PERSON_SEGMENT_CATEGORIES))
    base = parent_asset.title or Path(parent_asset.filename).stem
    return (
        "Clasifica este clip derivado de un video largo de personas para organizarlo en Drive. "
        "Responde solo JSON pequeño con title, filename_slug, category, keywords, confidence y needs_review. "
        f"Categorias permitidas: {allowed}. "
        "Prefiere women, men, couples, family, business, health o spiritual solo cuando sea claro; si dudas usa other. "
        "El title debe ser corto en espanol. filename_slug debe estar sin acentos, lowercase y con underscores. "
        f"Asset padre: {base}. Segmento: {plan.index:03d}, {plan.start_seconds:.1f}s-{plan.end_seconds:.1f}s."
    )


def call_openai_segment_naming(prompt: str, image_paths: list[Path]) -> SegmentNamingPayload:
    settings = get_settings()
    if not settings.openai_api_key:
        raise LongVideoSegmentationError("OPENAI_API_KEY is not configured")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LongVideoSegmentationError("openai package is not installed") from exc

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    content.extend({"type": "input_image", "image_url": image_to_data_url(path)} for path in image_paths)
    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=45.0, max_retries=1)
        response = client.responses.create(
            model=settings.ai_model or DEFAULT_OPENAI_MODEL,
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "segment_naming",
                    "schema": segment_naming_json_schema(),
                    "strict": True,
                }
            },
        )
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            output_text = json.dumps(response.model_dump() if hasattr(response, "model_dump") else response)
        return SegmentNamingPayload.model_validate_json(output_text)
    except Exception as exc:
        raise LongVideoSegmentationError(sanitize_provider_error(str(exc))) from exc


def segment_naming_json_schema() -> dict[str, Any]:
    schema = SegmentNamingPayload.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = ["title", "filename_slug", "category", "keywords", "confidence", "needs_review"]
    for property_schema in schema.get("properties", {}).values():
        if isinstance(property_schema, dict):
            property_schema.pop("default", None)
    return schema


def upload_segment_to_drive(local_clip_path: Path, remote_name: str, remote_path: str) -> None:
    RcloneService().copyto_local_to_remote(str(local_clip_path), remote_name, remote_path)


def validate_rclone_remote_exists(remote: str, role: str = "rclone") -> None:
    clean_remote = clean_text(remote)
    if not clean_remote:
        raise LongVideoSegmentationError(f"Missing {role} rclone remote")
    try:
        if not RcloneService().remote_exists(clean_remote):
            raise LongVideoSegmentationError(
                f"{role.capitalize()} rclone remote not found: {clean_remote}"
            )
    except LongVideoSegmentationError:
        raise
    except Exception as exc:
        raise LongVideoSegmentationError(f"Could not validate {role} rclone remote: {exc}") from exc


def create_derived_asset(
    session: Session,
    parent: Asset,
    source: Source,
    plan: SegmentPlan,
    title: str,
    output_filename: str,
    category: str,
    remote: str,
    remote_path: str,
    naming: SegmentNaming | None = None,
    raw_video_id: int | None = None,
) -> Asset:
    existing = session.scalar(select(Asset).where(Asset.source_id == source.id, Asset.remote_path == remote_path))
    if existing is not None:
        existing.has_audio = False
        existing.source_video = parent.remote_path
        if raw_video_id is not None:
            existing.raw_video_id = raw_video_id
        apply_segment_naming_metadata(session, existing, category, naming)
        return existing
    search_text, embedding_text = segment_search_text(parent, title, category, naming)
    child = Asset(
        asset_uid=f"{parent.asset_uid}-seg-{plan.index:03d}",
        source=source,
        provider=parent.provider,
        rclone_remote=remote,
        remote_path=remote_path,
        source_video=parent.remote_path,
        raw_video_id=raw_video_id,
        source_path=remote_path,
        filename=output_filename,
        file_ext=".mp4",
        mime_type="video/mp4",
        type="video",
        brand_id=parent.brand_id,
        product_id=parent.product_id,
        title=title,
        orientation=parent.orientation,
        duration_seconds=plan.duration_seconds,
        width=parent.width,
        height=parent.height,
        fps=parent.fps,
        has_audio=False,
        status="active",
        source_status="active",
        preview_status="pending",
        technical_metadata_status="pending",
        ai_enrichment_status="pending",
        usage_scope=parent.usage_scope,
        rights_status=parent.rights_status,
        auto_select_enabled=True,
        parent_asset=parent,
        is_derivative=True,
        derivative_type="segment",
        segment_index=plan.index,
        segment_start_seconds=plan.start_seconds,
        segment_end_seconds=plan.end_seconds,
        search_text=search_text,
        embedding_text=embedding_text,
        ai_enrichment_confidence=naming.confidence if naming else None,
        needs_human_review=naming.needs_review if naming else False,
        review_reason="Segment naming confidence below threshold" if naming and naming.needs_review else None,
    )
    child.niches = list(parent.niches)
    session.add(child)
    session.flush()
    apply_segment_naming_metadata(session, child, category, naming)
    return child


def apply_segment_naming_metadata(
    session: Session,
    asset: Asset,
    category: str,
    naming: SegmentNaming | None,
) -> None:
    if naming is None:
        return
    asset.title = trunc_text(naming.title, 500)
    asset.ai_enrichment_confidence = naming.confidence
    asset.needs_human_review = naming.needs_review
    if naming.needs_review:
        asset.review_reason = asset.review_reason or "Segment naming confidence below threshold"
    asset.search_text, asset.embedding_text = segment_search_text(asset.parent_asset or asset, naming.title, category, naming)
    add_segment_keywords(session, asset, naming)


def segment_search_text(
    parent: Asset,
    title: str,
    category: str,
    naming: SegmentNaming | None,
) -> tuple[str, str]:
    keywords = " ".join(naming.keywords) if naming else ""
    parent_text = parent.search_text or parent.filename
    search_text = " ".join(part for part in [title, category, keywords, parent_text] if part)
    embedding_text = " ".join(part for part in [title, keywords, parent_text] if part)
    return search_text.strip(), embedding_text.strip()


def add_segment_keywords(session: Session, asset: Asset, naming: SegmentNaming) -> None:
    if not naming.keywords:
        return
    existing = {
        (keyword.keyword, keyword.category, keyword.language)
        for keyword in asset.keywords
        if keyword.source == "ai"
    }
    for keyword in naming.keywords:
        key = (keyword, "segment", "es")
        if key in existing:
            continue
        session.add(
            AssetKeyword(
                asset=asset,
                keyword=keyword,
                category="segment",
                weight=0.7,
                confidence=naming.confidence,
                source="ai",
                language="es",
            )
        )


def mark_parent_segmented(
    session: Session,
    parent_asset_id: int,
    segment_count: int,
    created_count: int,
    failed_count: int,
) -> None:
    parent = session.get(Asset, parent_asset_id)
    if parent is None:
        return
    parent.segment_count = created_count
    parent.segmented_at = datetime.now(UTC)
    parent.auto_select_enabled = False
    parent.delete_original_eligible = created_count > 0
    if created_count and not failed_count:
        parent.segmentation_status = "ready"
    elif created_count:
        parent.segmentation_status = "partial"
    else:
        parent.segmentation_status = "failed"
    session.commit()


def approve_segmentation(session: Session, parent_asset_id: int, approved_by: str | None = None) -> Asset:
    parent = session.get(Asset, parent_asset_id)
    if parent is None:
        raise LongVideoSegmentationError(f"Asset not found: {parent_asset_id}")
    if not has_valid_children(session, parent.id):
        raise LongVideoSegmentationError("Segmentation has no active child assets")
    parent.segmentation_approved_at = datetime.now(UTC)
    parent.segmentation_approved_by = approved_by
    session.commit()
    return parent


def delete_original_after_approval(
    session: Session,
    parent_asset_id: int,
    force: bool = False,
) -> Asset:
    parent = session.get(Asset, parent_asset_id)
    if parent is None:
        raise LongVideoSegmentationError(f"Asset not found: {parent_asset_id}")
    if parent.is_derivative:
        raise LongVideoSegmentationError("Derivative assets cannot delete originals")
    if parent.segmentation_status not in {"ready", "partial"}:
        raise LongVideoSegmentationError("Parent segmentation is not ready")
    if not parent.delete_original_eligible:
        raise LongVideoSegmentationError("Parent is not eligible for original deletion")
    if parent.segmentation_approved_at is None and not force:
        parent.original_delete_status = "skipped"
        session.commit()
        raise LongVideoSegmentationError("Segmentation must be approved before deleting original")
    if not has_valid_children(session, parent.id):
        raise LongVideoSegmentationError("No active child assets found")
    try:
        RcloneService().delete_remote_file(parent.rclone_remote or "", parent.remote_path)
        parent.original_delete_status = "deleted"
        parent.original_deleted_at = datetime.now(UTC)
        parent.original_delete_error = None
        parent.source_status = "deleted"
        parent.auto_select_enabled = False
        session.commit()
        return parent
    except Exception as exc:
        session.rollback()
        failed_parent = session.get(Asset, parent_asset_id)
        if failed_parent is None:
            raise
        failed_parent.original_delete_status = "failed"
        failed_parent.original_delete_error = sanitize_segmentation_error(str(exc))
        session.commit()
        return failed_parent


def get_or_create_derived_source(session: Session, parent: Asset, remote: str, root: str) -> Source:
    public_id = parent.source.derived_source_id if parent.source and parent.source.derived_source_id else None
    public_id = clean_text(public_id) or f"{parent.source.source_id}_derived"
    source = session.scalar(select(Source).where(Source.source_id == public_id))
    if source is not None:
        return source
    source = Source(
        source_id=public_id,
        provider=parent.provider,
        label=f"{parent.source.label} derived b-rolls",
        rclone_remote=remote,
        root_path=root,
        source_role="derived_broll",
        sync_enabled=True,
        enabled=True,
    )
    session.add(source)
    session.flush()
    return source


def generate_child_enrichment(session: Session, child_id: int, skip_ai: bool = False) -> None:
    generate_asset_preview(session, child_id, force=False)
    if not skip_ai:
        enrich_asset_with_ai(session, child_id, force=False)


def fail_run(
    session: Session,
    parent: Asset,
    run: AssetSegmentationRun,
    message: str,
    extra_report: dict[str, Any] | None = None,
) -> None:
    safe = sanitize_segmentation_error(message)
    parent.segmentation_status = "failed"
    parent.auto_select_enabled = False
    run.status = "failed"
    run.completed_at = datetime.now(UTC)
    run.error = safe
    run.report_json = {"children_created": 0, "error": safe}
    if extra_report:
        run.report_json.update(extra_report)
    session.commit()


def segmentation_config() -> dict[str, object]:
    settings = get_settings()
    return {
        "min_seconds": settings.segment_min_seconds,
        "target_seconds": settings.segment_target_seconds,
        "max_seconds": settings.segment_max_seconds,
        "scene_threshold": settings.segment_scene_threshold,
        "output_mode": settings.segment_output_mode,
        "output_crf": settings.segment_output_crf,
        "output_preset": settings.segment_output_preset,
        "strip_audio": settings.segment_strip_audio,
    }


def probe_duration(input_path: Path) -> float:
    data = run_ffprobe(input_path)
    duration = None
    format_data = data.get("format")
    if isinstance(format_data, dict):
        duration = parse_float(format_data.get("duration"))
    return duration or 0.0


def detect_scene_segments(input_path: Path, duration: float, config: dict[str, object]) -> list[SegmentPlan]:
    command = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-vf",
        f"select='gt(scene,{config['scene_threshold']})',showinfo",
        "-f",
        "null",
        "-",
    ]
    try:
        result = run_subprocess(command, "ffmpeg scene detection", timeout=600, check=False)
    except LongVideoSegmentationError:
        return []
    output = f"{result.stderr}\n{result.stdout}"
    scene_times = [0.0]
    for match in re.finditer(r"pts_time:([0-9.]+)", output):
        value = parse_float(match.group(1))
        if value is not None and 0 < value < duration:
            scene_times.append(value)
    scene_times.append(duration)
    scene_times = sorted(set(round(value, 3) for value in scene_times))
    return build_plans_from_boundaries(scene_times, config)


def build_plans_from_boundaries(boundaries: list[float], config: dict[str, object]) -> list[SegmentPlan]:
    min_seconds = float(config["min_seconds"])
    max_seconds = float(config["max_seconds"])
    plans: list[SegmentPlan] = []
    index = 1
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        duration = end - start
        if duration < min_seconds:
            continue
        if duration > max_seconds:
            end = start + max_seconds
        plans.append(SegmentPlan(index=index, start_seconds=start, end_seconds=end))
        index += 1
    return plans


def fallback_window_segments(duration: float, config: dict[str, object]) -> list[SegmentPlan]:
    min_seconds = float(config["min_seconds"])
    target_seconds = float(config["target_seconds"])
    max_seconds = float(config["max_seconds"])
    step = max(min_seconds, min(target_seconds, max_seconds))
    plans: list[SegmentPlan] = []
    start = 0.0
    index = 1
    while start < duration:
        end = min(start + step, duration)
        if end - start >= min_seconds:
            plans.append(SegmentPlan(index=index, start_seconds=round(start, 3), end_seconds=round(end, 3)))
            index += 1
        start += step
    return plans


def renumber_plans(plans: list[SegmentPlan]) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=index, start_seconds=plan.start_seconds, end_seconds=plan.end_seconds)
        for index, plan in enumerate(plans, start=1)
    ]


def run_subprocess(
    command: list[str],
    operation: str,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise LongVideoSegmentationError(f"{operation} binary was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise LongVideoSegmentationError(f"{operation} timed out") from exc
    if check and result.returncode != 0:
        raise LongVideoSegmentationError(
            f"{operation} failed: {sanitize_segmentation_error(result.stderr or result.stdout)}"
        )
    return result


def allowed_categories() -> set[str]:
    return {item.strip().lower() for item in get_settings().derived_asset_category_folders.split(",") if item.strip()}


def safe_segment_filename(slug: str, index: int) -> str:
    cleaned = safe_slug(slug)
    if f"segment_{index:03d}" not in cleaned:
        cleaned = f"{cleaned}_segment_{index:03d}"
    return f"{cleaned[:180]}.mp4"


def safe_slug(value: str) -> str:
    return slugify_es(value)


def safe_keyword(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip().lower())
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = re.sub(r"[^\w áéíóúüñ-]", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned[:80] or None


def trunc_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    return value[:max_length]


def safe_filename(filename: str) -> str:
    cleaned = slugify_es(Path(filename).name)
    return cleaned[:180] or "asset"


def slugify_es(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    stem = Path(ascii_value).stem
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return slug or "clip"


def resolve_derived_root(cli_derived_root: str | None, source: Source | None) -> str:
    if cli_derived_root is not None:
        return normalize_remote_root(cli_derived_root)
    if source is not None and source.derived_root_path is not None:
        return normalize_remote_root(source.derived_root_path)
    return ""


def build_derived_remote_path(derived_root: str | None, category: str, filename: str) -> str:
    clean_category = classify_segment_category(category)
    clean_filename = validate_remote_path_part(filename, "filename")
    clean_root = normalize_remote_root(derived_root)
    parts = [clean_root, clean_category, clean_filename] if clean_root else [clean_category, clean_filename]
    return str(PurePosixPath(*parts))


def normalize_remote_root(root: str | None) -> str:
    if root is None:
        return ""
    clean_parts = [validate_remote_path_part(part, "derived root") for part in root.split("/") if part]
    return str(PurePosixPath(*clean_parts)) if clean_parts else ""


def validate_remote_path_part(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise LongVideoSegmentationError(f"Invalid {label}: empty path part")
    if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise LongVideoSegmentationError(f"Invalid {label}: path traversal is not allowed")
    return cleaned


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def parse_float(value: object) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def sanitize_segmentation_error(message: str) -> str:
    sanitized = sanitize_error_message(sanitize_rclone_message(message))
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized[:700] or "Long video segmentation failed"


def has_valid_children(session: Session, parent_asset_id: int) -> bool:
    count = session.scalar(
        select(func.count())
        .select_from(Asset)
        .where(
            Asset.parent_asset_id == parent_asset_id,
            Asset.is_derivative.is_(True),
            Asset.status == "active",
            Asset.source_status == "active",
        )
    )
    return bool(count)


def run_to_summary(run: AssetSegmentationRun) -> dict[str, object]:
    report = run.report_json if isinstance(run.report_json, dict) else {}
    return {
        "run_uid": run.run_uid,
        "status": run.status,
        "children_created": report.get("children_created", 0),
        "report": json.loads(json.dumps(report, default=str)),
    }
