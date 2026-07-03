import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Asset,
    AssetCollection,
    AssetNiche,
    AssetTag,
    AssetUsage,
    AuthProfile,
    Brand,
    Collection,
    JobAssetBundle,
    Niche,
    Product,
    Source,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


def make_asset_graph(session: Session) -> Asset:
    brand = Brand(slug="kurukin", name="Kurukin")
    product = Product(brand=brand, slug="starter", name="Starter")
    niche = Niche(slug="wellness", name="Wellness")
    auth_profile = AuthProfile(
        auth_profile_id="auth-google-drive-main",
        provider="google_drive",
        label="Google Drive main",
        auth_type="rclone",
        secret_ref="secret://asset-hub/google-drive-main",
    )
    source = Source(
        source_id="drive-main",
        provider="google_drive",
        label="Drive main",
        auth_profile=auth_profile,
        rclone_remote="gdrive",
        root_path="/assets",
    )
    asset = Asset(
        asset_uid="asset-001",
        source=source,
        provider="google_drive",
        remote_path="/assets/video-001.mp4",
        filename="video-001.mp4",
        brand=brand,
        product=product,
    )
    asset.niches.append(niche)
    session.add(asset)
    session.flush()
    return asset


def test_import_all_catalog_models() -> None:
    models = {
        AuthProfile,
        Source,
        Brand,
        Product,
        Niche,
        Collection,
        Asset,
        AssetTag,
        AssetNiche,
        AssetCollection,
        AssetUsage,
        JobAssetBundle,
    }

    assert len(models) == 12


def test_create_brand_product_niche_auth_profile_source_asset(session: Session) -> None:
    asset = make_asset_graph(session)

    assert asset.id is not None
    assert asset.source.auth_profile is not None
    assert asset.brand is not None
    assert asset.product is not None
    assert asset.niches[0].slug == "wellness"


def test_asset_defaults(session: Session) -> None:
    asset = make_asset_graph(session)

    assert asset.flip_horizontal_allowed is True
    assert asset.flip_vertical_allowed is False
    assert asset.crop_allowed is True
    assert asset.zoom_allowed is True
    assert asset.reverse_allowed is False
    assert asset.safe_for_subtitles is True
    assert asset.overlay_safe_area == "unknown"


def test_asset_unique_source_id_remote_path_constraint(session: Session) -> None:
    asset = make_asset_graph(session)
    duplicate = Asset(
        asset_uid="asset-002",
        source=asset.source,
        provider="google_drive",
        remote_path=asset.remote_path,
        filename="duplicate.mp4",
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.flush()


def test_asset_tag_relationship(session: Session) -> None:
    asset = make_asset_graph(session)
    asset.tags.append(AssetTag(tag="hook"))
    session.flush()

    saved_asset = session.scalar(select(Asset).where(Asset.id == asset.id))
    assert saved_asset is not None
    assert [tag.tag for tag in saved_asset.tags] == ["hook"]


def test_asset_usage_creatable_and_count_incrementable(session: Session) -> None:
    asset = make_asset_graph(session)
    asset.usages.append(AssetUsage(job_id="job-001", renderer="mpt"))
    asset.usage_count += 1
    session.flush()

    assert asset.usage_count == 1
    assert asset.usages[0].job_id == "job-001"
