from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, AssetKeyword, AssetTag, Brand, Niche, Product, Source

VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "avi", "mkv"})
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png"})
AUDIO_EXTENSIONS = frozenset({"mp3", "wav"})
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS

VISIBLE_TEXT_TOKENS = frozenset(
    {"texto", "text", "caption", "subtitle", "subtitulo", "screen", "pantalla"}
)
LOGO_TOKENS = frozenset({"logo", "marca"})
WATERMARK_TOKENS = frozenset({"watermark"})
TEXT_LOGO_WATERMARK_TOKENS = VISIBLE_TEXT_TOKENS | LOGO_TOKENS | WATERMARK_TOKENS
TOKEN_SPLIT_RE = re.compile(r"[\s_-]+")
KEYWORD_SPLIT_RE = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True)
class AssetIndexContext:
    source_id: str
    remote: str
    root: str
    brand_slug: str | None = None
    product_slug: str | None = None
    default_niche_slug: str | None = None
    provider: str = "google_drive"


@dataclass(frozen=True)
class IndexSummary:
    total_listed: int = 0
    total_allowed: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class TextLogoWatermarkInference:
    has_visible_text: bool
    has_logo: bool
    has_watermark: bool
    flip_horizontal_allowed: bool


@dataclass(frozen=True)
class InferredKeyword:
    keyword: str
    category: str
    weight: float
    confidence: float
    source: str = "indexer"
    language: str = "und"


class AssetIndexer:
    def __init__(self, session: Session) -> None:
        self.session = session

    def index_entries(
        self,
        entries: list[dict[str, Any]],
        context: AssetIndexContext,
    ) -> IndexSummary:
        source = self._upsert_source(context)
        self.session.flush()
        brand = self._find_brand(context.brand_slug)
        product = self._find_product(context.product_slug, brand)
        niche = self._find_niche(context.default_niche_slug)

        created = 0
        updated = 0
        allowed = 0
        skipped = 0

        for entry in entries:
            file_ext = infer_file_ext(entry)
            if file_ext not in ALLOWED_EXTENSIONS:
                skipped += 1
                continue

            allowed += 1
            was_created = self._upsert_asset(
                entry=entry,
                context=context,
                source=source,
                brand=brand,
                product=product,
                niche=niche,
                file_ext=file_ext,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return IndexSummary(
            total_listed=len(entries),
            total_allowed=allowed,
            created=created,
            updated=updated,
            skipped=skipped,
        )

    def _upsert_source(self, context: AssetIndexContext) -> Source:
        source = self.session.scalar(
            select(Source).where(Source.source_id == context.source_id)
        )
        label = context.source_id.replace("_", " ").replace("-", " ").title()
        if source is None:
            source = Source(
                source_id=context.source_id,
                provider=context.provider,
                label=label,
            )
            self.session.add(source)

        source.provider = context.provider
        source.rclone_remote = context.remote
        source.root_path = context.root
        source.enabled = True
        return source

    def _find_brand(self, slug: str | None) -> Brand | None:
        if not slug:
            return None
        brand = self.session.scalar(select(Brand).where(Brand.slug == slug))
        if brand is None:
            raise ValueError(f"Brand not found for slug: {slug}")
        return brand

    def _find_product(self, slug: str | None, brand: Brand | None) -> Product | None:
        if not slug:
            return None
        if brand is None:
            raise ValueError("--product requires --brand so product lookup stays scoped")
        product = self.session.scalar(
            select(Product).where(Product.brand_id == brand.id, Product.slug == slug)
        )
        if product is None:
            raise ValueError(f"Product not found for slug: {slug} under brand: {brand.slug}")
        return product

    def _find_niche(self, slug: str | None) -> Niche | None:
        if not slug:
            return None
        niche = self.session.scalar(select(Niche).where(Niche.slug == slug))
        if niche is None:
            raise ValueError(f"Niche not found for slug: {slug}")
        return niche

    def _upsert_asset(
        self,
        entry: dict[str, Any],
        context: AssetIndexContext,
        source: Source,
        brand: Brand | None,
        product: Product | None,
        niche: Niche | None,
        file_ext: str,
    ) -> bool:
        remote_path = build_remote_path(context.root, entry)
        asset = self.session.scalar(
            select(Asset).where(Asset.source == source, Asset.remote_path == remote_path)
        )
        created = asset is None
        filename = infer_filename(entry)
        asset_type = infer_asset_type(file_ext)
        tags = infer_tags(filename)
        text_inference = infer_text_logo_watermark(tags)
        generated_at = datetime.now(UTC)
        usage_scope = infer_default_usage_scope(brand=brand, asset_type=asset_type)

        if asset is None:
            asset = Asset(
                asset_uid=stable_asset_uid(context.source_id, remote_path),
                source=source,
                remote_path=remote_path,
                filename=filename,
                provider=context.provider,
                status="active",
                orientation="unknown",
                usage_scope=usage_scope,
                auto_select_enabled=usage_scope != "restricted",
            )
            self.session.add(asset)

        asset.provider = context.provider
        asset.rclone_remote = context.remote
        asset.source_path = remote_path
        asset.filename = filename
        asset.file_ext = file_ext
        asset.mime_type = entry.get("MimeType")
        asset.type = asset_type
        asset.size_bytes = normalize_size(entry.get("Size"))
        asset.brand = brand
        asset.product = product
        asset.status = "active"
        asset.last_indexed_at = generated_at
        if asset.usage_scope in {"global", "brand_exclusive"}:
            asset.usage_scope = usage_scope
        if asset.usage_scope == "restricted":
            asset.auto_select_enabled = False
        asset.search_text = build_search_text(
            asset,
            source=source,
            brand=brand,
            product=product,
            niche=niche,
        )
        asset.embedding_text = build_embedding_text(
            asset,
            source=source,
            brand=brand,
            product=product,
            niche=niche,
        )
        asset.auto_keywords_generated_at = generated_at

        if text_inference.has_visible_text:
            asset.has_visible_text = True
        if text_inference.has_logo:
            asset.has_logo = True
        if text_inference.has_watermark:
            asset.has_watermark = True
        if not text_inference.flip_horizontal_allowed:
            asset.flip_horizontal_allowed = False

        if niche is not None and niche not in asset.niches:
            asset.niches.append(niche)

        existing_tags = {asset_tag.tag for asset_tag in asset.tags}
        for tag in tags - existing_tags:
            asset.tags.append(AssetTag(tag=tag))

        refresh_indexer_keywords(
            asset,
            infer_keywords(
                filename=filename,
                source=source,
                brand=brand,
                product=product,
                niche=niche,
            ),
        )

        return created


def infer_file_ext(entry: dict[str, Any]) -> str:
    filename = infer_filename(entry)
    suffix = PurePosixPath(filename).suffix
    return suffix.lstrip(".").lower()


def infer_asset_type(file_ext: str) -> str:
    normalized = file_ext.lower().lstrip(".")
    if normalized in VIDEO_EXTENSIONS:
        return "video"
    if normalized in IMAGE_EXTENSIONS:
        return "image"
    if normalized in AUDIO_EXTENSIONS:
        return "audio"
    return "unknown"


def infer_filename(entry: dict[str, Any]) -> str:
    name = entry.get("Name")
    if isinstance(name, str) and name:
        return name
    path = entry.get("Path")
    if isinstance(path, str) and path:
        return PurePosixPath(path).name
    raise ValueError("rclone entry is missing Name and Path")


def infer_tags(filename: str) -> set[str]:
    stem = PurePosixPath(filename).stem.lower()
    raw_tokens = TOKEN_SPLIT_RE.split(stem)
    return {token for token in raw_tokens if len(token) > 2}


def infer_default_usage_scope(brand: Brand | None, asset_type: str) -> str:
    if asset_type == "audio":
        return "global"
    if brand is not None and asset_type in {"video", "image"}:
        return "brand_exclusive"
    return "global"


def infer_keywords(
    filename: str,
    source: Source,
    brand: Brand | None,
    product: Product | None,
    niche: Niche | None,
) -> list[InferredKeyword]:
    candidates: list[InferredKeyword] = []
    candidates.extend(
        InferredKeyword(token, "filename", 1.0, 0.95)
        for token in tokenize_keyword_text(PurePosixPath(filename).stem)
    )
    candidates.extend(
        InferredKeyword(token, "source", 0.65, 0.85)
        for token in tokenize_keyword_values(
            source.source_id,
            source.label,
            source.rclone_remote,
            source.root_path,
        )
    )
    if brand is not None:
        candidates.extend(
            InferredKeyword(token, "brand", 1.25, 0.95)
            for token in tokenize_keyword_values(brand.slug, brand.name)
        )
    if product is not None:
        candidates.extend(
            InferredKeyword(token, "product", 1.1, 0.9)
            for token in tokenize_keyword_values(product.slug, product.name)
        )
    if niche is not None:
        candidates.extend(
            InferredKeyword(token, "niche", 1.0, 0.9)
            for token in tokenize_keyword_values(niche.slug, niche.name)
        )

    deduped: dict[tuple[str, str, str], InferredKeyword] = {}
    for candidate in candidates:
        key = (candidate.keyword, candidate.category, candidate.language)
        previous = deduped.get(key)
        if previous is None or candidate.weight > previous.weight:
            deduped[key] = candidate
    return sorted(deduped.values(), key=lambda item: (item.category, item.keyword))


def tokenize_keyword_text(value: str | None, keep_compound: bool = False) -> set[str]:
    if not value:
        return set()
    normalized = value.lower().strip()
    raw_tokens = KEYWORD_SPLIT_RE.split(normalized)
    tokens = {token for token in raw_tokens if len(token) > 2}
    if keep_compound:
        compound = re.sub(r"[\W]+", "_", normalized).strip("_")
        if len(compound) > 2:
            tokens.add(compound)
    return tokens


def tokenize_keyword_values(*values: str | None) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(tokenize_keyword_text(value, keep_compound=True))
    return tokens


def refresh_indexer_keywords(asset: Asset, keywords: list[InferredKeyword]) -> None:
    expected_keys = {
        (keyword.keyword, keyword.category, keyword.language)
        for keyword in keywords
    }
    existing_by_key = {
        (keyword.keyword, keyword.category, keyword.language): keyword
        for keyword in asset.keywords
        if keyword.source == "indexer"
    }

    for existing in list(asset.keywords):
        key = (existing.keyword, existing.category, existing.language)
        if existing.source == "indexer" and key not in expected_keys:
            asset.keywords.remove(existing)

    for keyword in keywords:
        key = (keyword.keyword, keyword.category, keyword.language)
        existing = existing_by_key.get(key)
        if existing is None:
            asset.keywords.append(
                AssetKeyword(
                    keyword=keyword.keyword,
                    category=keyword.category,
                    weight=keyword.weight,
                    confidence=keyword.confidence,
                    source=keyword.source,
                    language=keyword.language,
                )
            )
            continue
        existing.weight = keyword.weight
        existing.confidence = keyword.confidence
        existing.source = keyword.source


def build_search_text(
    asset: Asset,
    source: Source,
    brand: Brand | None,
    product: Product | None,
    niche: Niche | None,
) -> str:
    parts = [
        asset.asset_uid,
        asset.filename,
        asset.remote_path,
        asset.title,
        asset.description,
        source.source_id,
        source.label,
        brand.slug if brand else None,
        brand.name if brand else None,
        product.slug if product else None,
        product.name if product else None,
        niche.slug if niche else None,
        niche.name if niche else None,
    ]
    return compact_text(parts)


def build_embedding_text(
    asset: Asset,
    source: Source,
    brand: Brand | None,
    product: Product | None,
    niche: Niche | None,
) -> str:
    parts = [
        asset.filename,
        asset.title,
        asset.description,
        asset.visual_description,
        asset.action_description,
        asset.best_for,
        brand.name if brand else None,
        product.name if product else None,
        niche.name if niche else None,
        source.label,
    ]
    return compact_text(parts)


def compact_text(parts: list[str | None]) -> str:
    return " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def infer_text_logo_watermark(tags: set[str]) -> TextLogoWatermarkInference:
    has_visible_text = bool(tags & VISIBLE_TEXT_TOKENS)
    has_logo = bool(tags & LOGO_TOKENS)
    has_watermark = bool(tags & WATERMARK_TOKENS)
    return TextLogoWatermarkInference(
        has_visible_text=has_visible_text,
        has_logo=has_logo,
        has_watermark=has_watermark,
        flip_horizontal_allowed=not (has_visible_text or has_logo or has_watermark),
    )


def build_remote_path(root: str, entry: dict[str, Any]) -> str:
    raw_path = entry.get("Path")
    if not isinstance(raw_path, str) or not raw_path:
        raw_path = infer_filename(entry)
    normalized_entry_path = raw_path.strip("/")
    normalized_root = root.strip("/")
    if normalized_root:
        return f"{normalized_root}/{normalized_entry_path}"
    return normalized_entry_path


def stable_asset_uid(source_id: str, remote_path: str) -> str:
    digest = sha256(f"{source_id}:{remote_path}".encode("utf-8")).hexdigest()[:24]
    return f"asset_{digest}"


def normalize_size(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
