import subprocess

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, Brand, Niche, Product, Source
from app.services.asset_indexer import (
    ALLOWED_EXTENSIONS,
    AssetIndexContext,
    AssetIndexer,
    infer_asset_type,
    infer_default_usage_scope,
    infer_keywords,
    infer_tags,
    infer_text_logo_watermark,
)
from app.services.rclone_service import RcloneService, sanitize_rclone_message


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


@pytest.fixture()
def catalog(session: Session) -> None:
    brand = Brand(slug="mujer_no_escribas", name="Mujer No Escribas")
    product = Product(brand=brand, slug="metodo_pausa", name="Metodo Pausa")
    niche = Niche(slug="relaciones", name="Relaciones")
    session.add_all([brand, product, niche])
    session.flush()


def context() -> AssetIndexContext:
    return AssetIndexContext(
        source_id="drive_mujer_no_escribas",
        remote="gdrive_mne",
        root="Assets Mujer No Escribas",
        brand_slug="mujer_no_escribas",
        product_slug="metodo_pausa",
        default_niche_slug="relaciones",
    )


def test_infer_asset_type() -> None:
    assert infer_asset_type("mp4") == "video"
    assert infer_asset_type("JPG") == "image"
    assert infer_asset_type(".wav") == "audio"
    assert infer_asset_type("pdf") == "unknown"


def test_allowed_extensions() -> None:
    assert {"mp4", "mov", "avi", "mkv", "jpg", "jpeg", "png", "mp3", "wav"} == set(
        ALLOWED_EXTENSIONS
    )


def test_tag_inference() -> None:
    assert infer_tags("Hook_relaciones-texto final.MP4") == {
        "hook",
        "relaciones",
        "texto",
        "final",
    }


def test_text_logo_watermark_inference_affects_flip_horizontal_allowed() -> None:
    inference = infer_text_logo_watermark({"hook", "caption", "logo", "watermark"})

    assert inference.has_visible_text is True
    assert inference.has_logo is True
    assert inference.has_watermark is True
    assert inference.flip_horizontal_allowed is False


def test_default_usage_scope_inference(session: Session, catalog: None) -> None:
    brand = session.scalar(select(Brand).where(Brand.slug == "mujer_no_escribas"))
    assert brand is not None

    assert infer_default_usage_scope(brand, "video") == "brand_exclusive"
    assert infer_default_usage_scope(brand, "image") == "brand_exclusive"
    assert infer_default_usage_scope(brand, "audio") == "global"
    assert infer_default_usage_scope(None, "video") == "global"


def test_upsert_creates_asset(session: Session, catalog: None) -> None:
    summary = AssetIndexer(session).index_entries(
        [{"Path": "videos/hook_texto.mp4", "Name": "hook_texto.mp4", "Size": 123}],
        context(),
    )
    session.flush()

    asset = session.scalar(select(Asset))
    assert asset is not None
    assert summary.created == 1
    assert asset.remote_path == "Assets Mujer No Escribas/videos/hook_texto.mp4"
    assert asset.asset_uid.startswith("asset_")
    assert asset.provider == "google_drive"
    assert asset.rclone_remote == "gdrive_mne"
    assert asset.file_ext == "mp4"
    assert asset.type == "video"
    assert asset.status == "active"
    assert asset.orientation == "unknown"
    assert asset.has_visible_text is True
    assert asset.flip_horizontal_allowed is False
    assert asset.brand is not None
    assert asset.product is not None
    assert asset.niches[0].slug == "relaciones"
    assert {tag.tag for tag in asset.tags} == {"hook", "texto"}
    assert asset.usage_scope == "brand_exclusive"
    assert asset.auto_select_enabled is True
    assert asset.search_text is not None
    assert "mujer_no_escribas" in asset.search_text
    assert asset.embedding_text is not None
    assert "Mujer No Escribas" in asset.embedding_text
    assert asset.auto_keywords_generated_at is not None
    assert {keyword.keyword for keyword in asset.keywords} >= {
        "hook",
        "texto",
        "mujer_no_escribas",
        "relaciones",
    }


def test_keyword_generation_uses_filename_source_brand_product_and_niche(
    session: Session,
    catalog: None,
) -> None:
    source = Source(
        source_id="drive_mujer_no_escribas",
        provider="google_drive",
        label="Drive Mujer No Escribas",
    )
    brand = session.scalar(select(Brand).where(Brand.slug == "mujer_no_escribas"))
    product = session.scalar(select(Product).where(Product.slug == "metodo_pausa"))
    niche = session.scalar(select(Niche).where(Niche.slug == "relaciones"))
    assert brand is not None
    assert product is not None
    assert niche is not None

    keywords = infer_keywords(
        filename="Hook_relaciones-texto final.MP4",
        source=source,
        brand=brand,
        product=product,
        niche=niche,
    )
    keyword_values = {keyword.keyword for keyword in keywords}

    assert {"hook", "relaciones", "texto", "drive_mujer_no_escribas"} <= keyword_values
    assert "mujer_no_escribas" in keyword_values
    assert "metodo_pausa" in keyword_values


def test_upsert_updates_existing_asset(session: Session, catalog: None) -> None:
    indexer = AssetIndexer(session)
    indexer.index_entries(
        [{"Path": "videos/hook.mp4", "Name": "hook.mp4", "Size": 123}],
        context(),
    )
    session.flush()
    asset = session.scalar(select(Asset))
    assert asset is not None
    original_uid = asset.asset_uid

    summary = indexer.index_entries(
        [{"Path": "videos/hook.mp4", "Name": "renamed_logo.mp4", "Size": 456}],
        context(),
    )
    session.flush()

    updated = session.scalar(select(Asset))
    assert updated is not None
    assert summary.updated == 1
    assert updated.id == asset.id
    assert updated.asset_uid == original_uid
    assert updated.filename == "renamed_logo.mp4"
    assert updated.size_bytes == 456
    assert updated.has_logo is True
    assert updated.flip_horizontal_allowed is False


def test_duplicate_source_remote_path_does_not_create_duplicate(
    session: Session,
    catalog: None,
) -> None:
    indexer = AssetIndexer(session)
    entry = {"Path": "videos/hook.mp4", "Name": "hook.mp4", "Size": 123}

    indexer.index_entries([entry], context())
    indexer.index_entries([entry], context())
    session.flush()

    assert session.scalars(select(Asset)).all()
    assert len(session.scalars(select(Asset)).all()) == 1


def test_rclone_lsjson_is_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        assert command == [
            "rclone",
            "lsjson",
            "gdrive_mne:Assets Mujer No Escribas",
            "--recursive",
            "--files-only",
        ]
        assert check is True
        assert capture_output is True
        assert text is True
        assert timeout == 300
        return subprocess.CompletedProcess(command, 0, stdout='[{"Path":"a.mp4","Name":"a.mp4"}]')

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert RcloneService().list_json("gdrive_mne", "Assets Mujer No Escribas") == [
        {"Path": "a.mp4", "Name": "a.mp4"}
    ]


def test_rclone_error_message_redacts_secrets() -> None:
    message = sanitize_rclone_message("token = abc123 client_secret: verysecret")

    assert "abc123" not in message
    assert "verysecret" not in message
    assert "[redacted]" in message
