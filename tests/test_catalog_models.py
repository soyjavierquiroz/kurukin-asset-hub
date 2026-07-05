import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Asset,
    AssetAllowedBrand,
    AssetCollection,
    AssetKeyword,
    AssetNiche,
    AssetTag,
    AssetUsage,
    AuthProfile,
    Brand,
    BrandAssetPolicy,
    Collection,
    JobAssetBundle,
    JobAssetBundleItem,
    Niche,
    Product,
    ProductAssetPolicy,
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
        AssetAllowedBrand,
        AssetTag,
        AssetKeyword,
        AssetNiche,
        AssetCollection,
        AssetUsage,
        JobAssetBundle,
        JobAssetBundleItem,
        BrandAssetPolicy,
        ProductAssetPolicy,
    }

    assert len(models) == 17


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
    assert asset.usage_scope == "global"
    assert asset.rights_status == "unknown"
    assert asset.auto_select_enabled is True


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


def test_asset_keyword_and_allowed_brand_relationships(session: Session) -> None:
    asset = make_asset_graph(session)
    allowed_brand = Brand(slug="partner", name="Partner")
    asset.keywords.append(
        AssetKeyword(
            keyword="hook",
            category="filename",
            weight=1.0,
            confidence=0.95,
            source="indexer",
            language="und",
        )
    )
    asset.allowed_brands.append(AssetAllowedBrand(brand=allowed_brand))
    session.flush()

    saved_asset = session.scalar(select(Asset).where(Asset.id == asset.id))
    assert saved_asset is not None
    assert saved_asset.keywords[0].keyword == "hook"
    assert saved_asset.allowed_brands[0].brand.slug == "partner"


def test_brand_asset_policy_defaults(session: Session) -> None:
    brand = Brand(slug="policy-brand", name="Policy Brand")
    brand.asset_policy = BrandAssetPolicy()
    session.add(brand)
    session.flush()

    assert brand.asset_policy is not None
    assert brand.asset_policy.allow_global_assets is True
    assert brand.asset_policy.allow_global_video is True
    assert brand.asset_policy.allow_global_image is True
    assert brand.asset_policy.allow_stock_assets is True
    assert brand.asset_policy.allow_stock_video is True
    assert brand.asset_policy.allow_stock_image is True
    assert brand.asset_policy.allow_stock_audio is True
    assert brand.asset_policy.require_brand_match_for_video is True
    assert brand.asset_policy.require_brand_match_for_image is True
    assert brand.asset_policy.allow_global_audio is True
    assert brand.asset_policy.default_asset_scope == "brand_exclusive"


def test_product_asset_policy_defaults_and_nullable_overrides(session: Session) -> None:
    brand = Brand(slug="product-policy-brand", name="Product Policy Brand")
    product = Product(brand=brand, slug="starter", name="Starter")
    product.asset_policy = ProductAssetPolicy()
    session.add(product)
    session.flush()

    assert product.asset_policy is not None
    assert product.asset_policy.inherit_brand_policy is True
    assert product.asset_policy.default_usage_scope is None
    assert product.asset_policy.allow_global_video is None
    assert product.asset_policy.allow_global_image is None
    assert product.asset_policy.allow_global_audio is None
    assert product.asset_policy.allow_stock_video is None
    assert product.asset_policy.allow_stock_image is None
    assert product.asset_policy.allow_stock_audio is None
    assert product.asset_policy.require_product_match_for_video is None
    assert product.asset_policy.require_product_match_for_image is None
    assert product.asset_policy.auto_select_enabled_default is None


def test_asset_usage_creatable_and_count_incrementable(session: Session) -> None:
    asset = make_asset_graph(session)
    asset.usages.append(AssetUsage(job_id="job-001", renderer="mpt"))
    asset.usage_count += 1
    session.flush()

    assert asset.usage_count == 1
    assert asset.usages[0].job_id == "job-001"
