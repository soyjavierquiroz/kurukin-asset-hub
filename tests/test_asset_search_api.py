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
    AssetAIAnalysis,
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


def add_people_analysis(
    session: Session,
    asset: Asset,
    visual_presentation: str,
    search_terms: list[str],
    person_visibility: str = "clear",
    confidence: float = 0.9,
) -> None:
    session.add(
        AssetAIAnalysis(
            asset=asset,
            model="test-model",
            provider="test",
            input_type="preview",
            prompt_version="test",
            result_json={
                "contains_people": visual_presentation != "not_applicable",
                "people_count": 2 if visual_presentation == "mixed" else 1,
                "visual_presentation": visual_presentation,
                "visual_presentation_confidence": confidence,
                "person_visibility": person_visibility,
                "search_terms": search_terms,
            },
            confidence=confidence,
        )
    )


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


def test_search_hombre_filters_masculine_and_excludes_feminine() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        add_people_analysis(session, seeded["own"], "masculine", ["persona", "hombre"])
        add_people_analysis(session, seeded["other_product"], "feminine", ["persona", "mujer"])
        session.commit()

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "q": "hombre", "limit": "10"})

    assert "asset-own" in uids
    assert "asset-other-product" not in uids


def test_search_mujer_filters_feminine_and_excludes_masculine() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        add_people_analysis(session, seeded["own"], "masculine", ["persona", "hombre"])
        add_people_analysis(session, seeded["other_product"], "feminine", ["persona", "mujer"])
        session.commit()

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "q": "mujer", "limit": "10"})

    assert "asset-other-product" in uids
    assert "asset-own" not in uids


def test_search_persona_returns_all_human_visual_presentations() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        add_people_analysis(session, seeded["own"], "masculine", ["persona", "hombre"])
        add_people_analysis(session, seeded["other_product"], "feminine", ["persona", "mujer"])
        add_people_analysis(session, seeded["keyword"], "mixed", ["personas", "hombres", "mujeres", "grupo"])
        add_people_analysis(session, seeded["mystic"], "unclear", ["persona", "silueta"], "silhouette", 0.4)
        add_people_analysis(session, seeded["global_video"], "not_applicable", [])
        session.commit()

    uids = get_asset_uids(client, {"brand_slug": "brand_a", "q": "persona", "limit": "20"})

    assert {"asset-own", "asset-other-product", "asset-keyword", "asset-mystic"}.issubset(uids)
    assert "asset-global-video" not in uids


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


def test_managed_brand_filter_does_not_mix_brands() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        brand_a = seeded["brand_a"]
        assert isinstance(brand_a, Brand)
        own = seeded["own"]
        other_brand = seeded["other_brand"]
        assert isinstance(own, Asset)
        assert isinstance(other_brand, Asset)
        own.scope = "brand"
        other_brand.scope = "brand"
        session.commit()

    uids = get_asset_uids(client, {"scope": "brand", "brand": brand_a.slug, "limit": "10"})

    assert "asset-own" in uids
    assert "asset-other-brand" not in uids


def test_managed_generic_filter_does_not_return_title_assets() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        global_video = seeded["global_video"]
        global_audio = seeded["global_audio"]
        assert isinstance(global_video, Asset)
        assert isinstance(global_audio, Asset)
        global_video.scope = "generic"
        global_audio.scope = "title"
        global_audio.title_type = "movie"
        global_audio.title_name = "Titulo de prueba"
        global_audio.title_slug = "titulo-de-prueba"
        session.commit()

    uids = get_asset_uids(client, {"scope": "generic", "limit": "10"})

    assert "asset-global-video" in uids
    assert "asset-global-audio" not in uids


def test_managed_title_filter_does_not_mix_titles() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seeded = seed_search_assets(session)
        global_video = seeded["global_video"]
        global_audio = seeded["global_audio"]
        assert isinstance(global_video, Asset)
        assert isinstance(global_audio, Asset)
        global_video.scope = "title"
        global_video.title_type = "movie"
        global_video.title_name = "Titulo de prueba"
        global_video.title_slug = "titulo-de-prueba"
        global_audio.scope = "title"
        global_audio.title_type = "movie"
        global_audio.title_name = "Otro titulo"
        global_audio.title_slug = "otro-titulo"
        session.commit()

    uids = get_asset_uids(client, {"scope": "title", "title": "titulo-de-prueba", "limit": "10"})

    assert "asset-global-video" in uids
    assert "asset-global-audio" not in uids


def seed_money_printer_assets(session: Session) -> None:
    source = Source(source_id="managed-drive", provider="google_drive", label="Managed Drive")
    grandiosa = Brand(slug="grandiosa-mujer", name="Grandiosa Mujer")
    other_brand = Brand(slug="otra-marca", name="Otra Marca")
    session.add_all([source, grandiosa, other_brand])
    session.flush()

    assets = [
        make_money_printer_asset(
            source,
            uid="generic-ready",
            filename="mujer_telefono_generic.mp4",
            scope="generic",
            search_text="mujer hablando por telefono generic",
        ),
        make_money_printer_asset(
            source,
            uid="generic-mirror",
            filename="mujer_espejo_generic.mp4",
            scope="generic",
            search_text="mujer maquillandose frente al espejo",
        ),
        make_money_printer_asset(
            source,
            uid="brand-grandiosa",
            filename="grandiosa_mujer_espejo.mp4",
            scope="brand",
            brand=grandiosa,
            search_text="mujer maquillandose frente al espejo grandiosa",
        ),
        make_money_printer_asset(
            source,
            uid="brand-other",
            filename="otra_marca_espejo.mp4",
            scope="brand",
            brand=other_brand,
            search_text="mujer maquillandose frente al espejo otra marca",
        ),
        make_money_printer_asset(
            source,
            uid="title-mi-otra-yo",
            filename="mi_otra_yo_telefono.mp4",
            scope="title",
            title_name="Mi Otra Yo",
            title_slug="mi-otra-yo",
            title_type="series",
            search_text="mujer hablando por telefono mi otra yo",
        ),
        make_money_printer_asset(
            source,
            uid="title-other",
            filename="otro_titulo_telefono.mp4",
            scope="title",
            title_name="Otro Titulo",
            title_slug="otro-titulo",
            title_type="series",
            search_text="mujer hablando por telefono otro titulo",
        ),
        make_money_printer_asset(
            source,
            uid="planned-generic",
            filename="planned_generic.mp4",
            scope="generic",
            status="move_planned",
            move_status="planned",
            search_text="mujer hablando por telefono planned",
        ),
        make_money_printer_asset(
            source,
            uid="review-generic",
            filename="review_generic.mp4",
            scope="generic",
            status="review_required",
            search_text="mujer hablando por telefono review",
        ),
        make_money_printer_asset(
            source,
            uid="failed-generic",
            filename="failed_generic.mp4",
            scope="generic",
            status="failed",
            move_status="move_failed",
            search_text="mujer hablando por telefono failed",
        ),
        make_money_printer_asset(
            source,
            uid="moved-status-generic",
            filename="moved_status_generic.mp4",
            scope="generic",
            status="moved",
            search_text="mujer hablando por telefono moved",
        ),
    ]
    for asset in (assets[1], assets[2], assets[3]):
        asset.primary_topic = "espejo"
    assets[0].keywords.append(AssetKeyword(keyword="telefono", category="object", weight=1.0, confidence=1.0))
    assets[0].keywords[0].source = "test"
    session.add_all(assets)
    session.commit()


def make_money_printer_asset(
    source: Source,
    *,
    uid: str,
    filename: str,
    scope: str,
    search_text: str,
    brand: Brand | None = None,
    title_name: str | None = None,
    title_slug: str | None = None,
    title_type: str | None = None,
    status: str = "ready",
    move_status: str = "moved",
) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        remote_path=f"30_assets/{filename}",
        drive_file_id=f"drive-{uid}",
        filename=filename,
        type="video",
        scope=scope,
        brand=brand,
        title_name=title_name,
        title_slug=title_slug,
        title_type=title_type,
        title=f"contexto {uid}",
        status=status,
        move_status=move_status,
        usage_scope="global",
        rights_status="owned",
        auto_select_enabled=True,
        orientation="9:16",
        primary_theme="personas",
        primary_topic="telefono",
        search_text=search_text,
    )


def post_money_printer_search(
    client: TestClient,
    payload: dict[str, object],
) -> dict[str, object]:
    response = client.post("/api/assets/search", json=payload, headers=API_HEADERS)
    assert response.status_code == 200
    return response.json()


def money_printer_asset_ids(response_json: dict[str, object]) -> set[str]:
    assets = response_json["assets"]
    assert isinstance(assets, list)
    return {asset["asset_id"] for asset in assets}


def test_money_printer_default_returns_only_generic() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "mujer telefono", "limit": 20})

    assert money_printer_asset_ids(result) == {"generic-ready", "moved-status-generic"}
    for asset in result["assets"]:
        assert asset["asset_id"] == asset["asset_uid"]
    assert result["source_policy"] == {"sources": [{"scope": "generic", "brand": None, "title": None}]}


def test_money_printer_strict_brand_does_not_mix_generic() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer espejo",
            "source_policy": {"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]},
        },
    )

    assert money_printer_asset_ids(result) == {"brand-grandiosa"}


def test_money_printer_strict_brand_does_not_mix_other_brand() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer espejo",
            "source_policy": {"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]},
        },
    )

    assert "brand-other" not in money_printer_asset_ids(result)


def test_money_printer_strict_title_does_not_mix_generic() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer telefono",
            "source_policy": {"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
        },
    )

    assert money_printer_asset_ids(result) == {"title-mi-otra-yo"}


def test_money_printer_strict_title_does_not_mix_other_title() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer telefono",
            "source_policy": {"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
        },
    )

    assert "title-other" not in money_printer_asset_ids(result)


def test_money_printer_generic_plus_title_returns_both() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer telefono",
            "source_policy": {
                "sources": [{"scope": "generic"}, {"scope": "title", "title": "mi-otra-yo"}]
            },
        },
    )

    assert money_printer_asset_ids(result) == {
        "generic-ready",
        "moved-status-generic",
        "title-mi-otra-yo",
    }


def test_money_printer_generic_plus_brand_returns_both() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "mujer espejo",
            "source_policy": {
                "sources": [{"scope": "generic"}, {"scope": "brand", "brand": "grandiosa-mujer"}]
            },
        },
    )

    assert money_printer_asset_ids(result) == {"generic-mirror", "brand-grandiosa"}


def test_money_printer_empty_source_policy_errors() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    response = client.post(
        "/api/assets/search",
        json={"source_policy": {"sources": []}},
        headers=API_HEADERS,
    )

    assert response.status_code == 422


def test_money_printer_brand_source_requires_brand() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    response = client.post(
        "/api/assets/search",
        json={"source_policy": {"sources": [{"scope": "brand"}]}},
        headers=API_HEADERS,
    )

    assert response.status_code == 422


def test_money_printer_title_source_requires_title() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    response = client.post(
        "/api/assets/search",
        json={"source_policy": {"sources": [{"scope": "title"}]}},
        headers=API_HEADERS,
    )

    assert response.status_code == 422


def test_money_printer_unknown_scope_errors() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    response = client.post(
        "/api/assets/search",
        json={"source_policy": {"sources": [{"scope": "global"}]}},
        headers=API_HEADERS,
    )

    assert response.status_code == 422


def test_money_printer_text_search_stays_inside_allowed_universe() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(
        client,
        {
            "query": "grandiosa",
            "source_policy": {"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]},
        },
    )

    assert money_printer_asset_ids(result) == {"brand-grandiosa"}


def test_money_printer_planned_does_not_appear() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "planned", "limit": 20})

    assert "planned-generic" not in money_printer_asset_ids(result)


def test_money_printer_review_required_does_not_appear() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "review", "limit": 20})

    assert "review-generic" not in money_printer_asset_ids(result)


def test_money_printer_failed_does_not_appear() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "failed", "limit": 20})

    assert "failed-generic" not in money_printer_asset_ids(result)


def test_money_printer_ready_and_moved_appear() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "mujer telefono", "limit": 20})

    assert {"generic-ready", "moved-status-generic"}.issubset(money_printer_asset_ids(result))


def test_money_printer_limit_is_respected() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_money_printer_assets(session)

    result = post_money_printer_search(client, {"query": "mujer telefono", "limit": 1})

    assert result["count"] == 1
    assert len(result["assets"]) == 1
