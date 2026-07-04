from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db_session
from app.main import create_app
from app.models import (
    Asset,
    AssetAllowedBrand,
    AssetKeyword,
    Brand,
    BrandAssetPolicy,
    Product,
    ProductAssetPolicy,
    Source,
)
from app.services.asset_search import normalize_search_text, score_asset_for_query, tokenize_query

API_HEADERS = {"X-Asset-Hub-Api-Key": "test-api-key"}


def make_test_client() -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_catalog_session() -> Generator[Session, None, None]:
        with session_factory() as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_catalog_session
    return TestClient(app), session_factory


def seed_search_assets(session: Session) -> dict[str, Asset | Brand | Product]:
    brand_a = Brand(slug="brand_a", name="Brand A")
    brand_a.asset_policy = BrandAssetPolicy(
        allow_global_video=True,
        allow_global_audio=True,
    )
    brand_b = Brand(slug="brand_b", name="Brand B")
    product_a = Product(brand=brand_a, slug="product_a", name="Product A")
    product_other = Product(brand=brand_a, slug="other_product", name="Other Product")
    product_b = Product(brand=brand_b, slug="product_b", name="Product B")
    source = Source(source_id="drive-main", provider="google_drive", label="Drive Main")
    session.add_all([brand_a, brand_b, product_a, product_other, product_b, source])
    session.flush()

    own = make_asset(
        source=source,
        uid="asset-own",
        filename="own_hook.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="brand_exclusive",
    )
    other_brand = make_asset(
        source=source,
        uid="asset-other-brand",
        filename="other_brand.mp4",
        brand=brand_b,
        product=product_b,
        usage_scope="brand_exclusive",
    )
    other_product = make_asset(
        source=source,
        uid="asset-other-product",
        filename="other_product.mp4",
        brand=brand_a,
        product=product_other,
        usage_scope="brand_exclusive",
    )
    global_video = make_asset(
        source=source,
        uid="asset-global-video",
        filename="global_broll.mp4",
        brand=None,
        product=None,
        usage_scope="global",
        asset_type="video",
    )
    global_audio = make_asset(
        source=source,
        uid="asset-global-audio",
        filename="global_music.mp3",
        brand=None,
        product=None,
        usage_scope="global",
        asset_type="audio",
    )
    allowed = make_asset(
        source=source,
        uid="asset-allowed",
        filename="partner_scene.mp4",
        brand=brand_b,
        product=product_b,
        usage_scope="allowed_brands",
    )
    allowed.allowed_brands.append(AssetAllowedBrand(brand=brand_a))
    restricted = make_asset(
        source=source,
        uid="asset-restricted",
        filename="restricted.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="restricted",
        auto_select_enabled=False,
    )
    disabled = make_asset(
        source=source,
        uid="asset-disabled",
        filename="disabled.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="brand_exclusive",
        auto_select_enabled=False,
    )
    keyword_asset = make_asset(
        source=source,
        uid="asset-keyword",
        filename="quiet_scene.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="brand_exclusive",
    )
    keyword_asset.keywords.append(
        AssetKeyword(
            keyword="retention",
            category="filename",
            weight=1.0,
            confidence=0.95,
            source="indexer",
            language="und",
        )
    )
    mystic = make_asset(
        source=source,
        uid="asset-mystic",
        filename="veyra_mistica_energia.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="brand_exclusive",
    )
    mystic.title = "Ritual mistico de energia"
    mystic.search_text = "mujer encapuchada mistica energia esoterico misterio"
    mystic.embedding_text = "Mujer encapuchada en escena de misticismo y energia espiritual."
    mystic.visual_description = "Una mujer encapuchada sostiene un celular en ambiente misterioso."
    mystic.best_for = "B-roll para narrativas de energia mistica y transformacion personal."
    mystic.keywords.extend(
        [
            AssetKeyword(
                keyword="místico",
                category="mood",
                weight=1.0,
                confidence=0.95,
                source="ai",
                language="es",
            ),
            AssetKeyword(
                keyword="energía",
                category="concept",
                weight=1.0,
                confidence=0.94,
                source="ai",
                language="es",
            ),
            AssetKeyword(
                keyword="celular",
                category="object",
                weight=0.8,
                confidence=0.9,
                source="ai",
                language="es",
            ),
        ]
    )
    inactive = make_asset(
        source=source,
        uid="asset-inactive",
        filename="inactive_mistica.mp4",
        brand=brand_a,
        product=product_a,
        usage_scope="brand_exclusive",
    )
    inactive.status = "inactive"

    session.add_all(
        [
            own,
            other_brand,
            other_product,
            global_video,
            global_audio,
            allowed,
            restricted,
            disabled,
            keyword_asset,
            mystic,
            inactive,
        ]
    )
    session.commit()
    return {
        "brand_a": brand_a,
        "product_a": product_a,
        "own": own,
        "other_brand": other_brand,
        "other_product": other_product,
        "global_video": global_video,
        "global_audio": global_audio,
        "allowed": allowed,
        "restricted": restricted,
        "disabled": disabled,
        "keyword": keyword_asset,
        "mystic": mystic,
        "inactive": inactive,
    }


def make_asset(
    source: Source,
    uid: str,
    filename: str,
    brand: Brand | None,
    product: Product | None,
    usage_scope: str,
    asset_type: str = "video",
    auto_select_enabled: bool = True,
) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        remote_path=f"/assets/{filename}",
        filename=filename,
        type=asset_type,
        brand=brand,
        product=product,
        status="active",
        usage_scope=usage_scope,
        auto_select_enabled=auto_select_enabled,
        search_text=filename,
    )


def asset_uids(response_json: dict[str, object]) -> set[str]:
    assets = response_json["assets"]
    assert isinstance(assets, list)
    return {asset["asset_uid"] for asset in assets}


def get_asset_uids(
    client: TestClient,
    params: dict[str, str] | None = None,
) -> set[str]:
    response = client.get("/api/assets/search", params=params or {}, headers=API_HEADERS)
    assert response.status_code == 200
    return asset_uids(response.json())


def test_normalize_search_text_removes_accents() -> None:
    assert normalize_search_text("mística energía") == "mistica energia"


def test_tokenize_query_normalizes_and_drops_tiny_tokens() -> None:
    assert tokenize_query("La mística energía 8") == ["mistica", "energia", "8"]


def test_score_matches_accented_keyword_without_accent_query() -> None:
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        seeded = seed_search_assets(session)
        asset = seeded["mystic"]
        assert isinstance(asset, Asset)

        score = score_asset_for_query(asset, tokenize_query("mistico energia"))

    assert score > 0


def test_search_requires_api_key() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    response = client.get("/api/assets/search", params={"brand_slug": "brand_a"})

    assert response.status_code == 401


def test_search_accepts_correct_api_key() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    response = client.get(
        "/api/assets/search",
        params={"brand_slug": "brand_a"},
        headers=API_HEADERS,
    )

    assert response.status_code == 200


def test_brand_exclusive_asset_does_not_appear_for_another_brand() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_b"})

    assert "asset-own" not in uids


def test_asset_from_same_brand_appears() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a"})

    assert "asset-own" in uids


def test_global_video_appears_when_brand_policy_allows_it() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "type": "video"})

    assert "asset-global-video" in uids


def test_global_video_does_not_appear_when_brand_policy_blocks_it() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        brand = seeded["brand_a"]
        assert isinstance(brand, Brand)
        assert brand.asset_policy is not None
        brand.asset_policy.allow_global_video = False
        session.commit()

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "type": "video"})

    assert "asset-global-video" not in uids


def test_global_audio_appears_when_brand_policy_allows_it() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "type": "audio"})

    assert "asset-global-audio" in uids


def test_product_policy_can_block_global_video_even_if_brand_allows_it() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        product = seeded["product_a"]
        assert isinstance(product, Product)
        product.asset_policy = ProductAssetPolicy(
            inherit_brand_policy=False,
            allow_global_video=False,
        )
        session.commit()

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_a", "product_slug": "product_a", "type": "video"},
    )

    assert "asset-global-video" not in uids


def test_product_policy_can_inherit_brand_policy() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        product = seeded["product_a"]
        assert isinstance(product, Product)
        product.asset_policy = ProductAssetPolicy(
            inherit_brand_policy=True,
            allow_global_video=False,
        )
        session.commit()

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_a", "product_slug": "product_a", "type": "video"},
    )

    assert "asset-global-video" in uids


def test_product_policy_can_require_product_match_for_video() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        product = seeded["product_a"]
        assert isinstance(product, Product)
        product.asset_policy = ProductAssetPolicy(
            inherit_brand_policy=False,
            require_product_match_for_video=True,
        )
        session.commit()

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_a", "product_slug": "product_a", "type": "video"},
    )

    assert "asset-own" in uids
    assert "asset-other-product" not in uids


def test_asset_allowed_brand_permits_exception() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a"})

    assert "asset-allowed" in uids


def test_restricted_and_disabled_assets_never_appear() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a"})

    assert "asset-restricted" not in uids
    assert "asset-disabled" not in uids


def test_search_by_keyword_works() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "q": "retention"})

    assert uids == {"asset-keyword"}


def test_search_with_brand_product_returns_product_assets_without_query() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_a", "product_slug": "product_a", "limit": "10"},
    )

    assert "asset-own" in uids
    assert "asset-mystic" in uids


def test_search_token_query_returns_related_asset() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    response = client.get(
        "/api/assets/search",
        params={
            "brand_slug": "brand_a",
            "product_slug": "product_a",
            "q": "mistica energia",
            "type": "video",
            "limit": "10",
        },
        headers=API_HEADERS,
    )

    assert response.status_code == 200
    assert "asset-mystic" in asset_uids(response.json())
    assert response.json()["assets"][0]["score"] > 0


def test_search_accented_query_matches_same_assets() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    base_params = {
        "brand_slug": "brand_a",
        "product_slug": "product_a",
        "type": "video",
        "limit": "10",
    }
    plain_uids = get_asset_uids(client, {**base_params, "q": "mistica energia"})
    accented_uids = get_asset_uids(client, {**base_params, "q": "mística energía"})

    assert accented_uids == plain_uids
    assert "asset-mystic" in accented_uids


def test_search_without_brand_does_not_return_brand_exclusive_assets() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(client, {"q": "mistica energia", "type": "video", "limit": "10"})

    assert "asset-mystic" not in uids


def test_search_wrong_brand_does_not_return_other_brand_exclusive_assets() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_b", "q": "veyra", "type": "video", "limit": "10"},
    )

    assert "asset-mystic" not in uids
    assert "asset-own" not in uids


def test_search_excludes_disabled_and_inactive_assets() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_search_assets(session)

    uids = get_asset_uids(
        client,
        {"brand_slug": "brand_a", "product_slug": "product_a", "q": "mistica", "limit": "10"},
    )

    assert "asset-disabled" not in uids
    assert "asset-inactive" not in uids
