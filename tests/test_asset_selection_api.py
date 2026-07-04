from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models import Asset, AssetKeyword, Brand, BrandAssetPolicy, Product, Source
from tests.test_asset_search_api import API_HEADERS, make_test_client


def seed_selection_assets(session: Session) -> dict[str, Asset | Brand | Product]:
    brand = Brand(slug="grandiosa_mujer", name="Grandiosa Mujer")
    brand.asset_policy = BrandAssetPolicy(allow_global_video=True, allow_global_image=True)
    other_brand = Brand(slug="otra_marca", name="Otra Marca")
    product = Product(brand=brand, slug="veyra", name="Veyra")
    other_product = Product(brand=other_brand, slug="otro_producto", name="Otro Producto")
    source = Source(
        source_id="drive-grandiosa",
        provider="google_drive",
        label="Grandiosa Drive",
        rclone_remote="gdrive_grandiosa",
    )
    session.add_all([brand, other_brand, product, other_product, source])
    session.flush()

    mystic = make_selection_asset(
        source=source,
        uid="asset-mystic",
        filename="veyra_mistica_energia.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    mystic.id = 8
    mystic.title = "Veyra energia mistica"
    mystic.search_text = "veyra energia mistica celular revelacion"
    mystic.best_for = "energia mistica hook broll"
    mystic.orientation = "9:16"
    mystic.preview_status = "ready"
    mystic.preview_path = "previews/asset-mystic/preview.mp4"
    mystic.thumbnail_path = "previews/asset-mystic/thumbnail.jpg"
    mystic.ai_enrichment_status = "ready"
    mystic.ai_enrichment_confidence = 0.95
    mystic.best_scene_role = "hook"
    mystic.safe_for_subtitles = True
    mystic.loopable = True
    mystic.similarity_group = "mystic-phone"
    mystic.duration_seconds = 5.4
    mystic.width = 1080
    mystic.height = 1920
    mystic.fps = 30.0
    mystic.codec = "h264"
    mystic.has_audio = True
    mystic.keywords.append(
        AssetKeyword(
            keyword="energía",
            category="concept",
            weight=1.0,
            confidence=0.95,
            source="ai",
            language="es",
        )
    )

    duplicate_group = make_selection_asset(
        source=source,
        uid="asset-mystic-alt",
        filename="veyra_mistica_alt.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    duplicate_group.id = 9
    duplicate_group.search_text = "veyra energia mistica alternativa"
    duplicate_group.orientation = "9:16"
    duplicate_group.preview_status = "ready"
    duplicate_group.ai_enrichment_status = "ready"
    duplicate_group.ai_enrichment_confidence = 0.9
    duplicate_group.similarity_group = "mystic-phone"

    phone = make_selection_asset(
        source=source,
        uid="asset-phone",
        filename="veyra_habla_telefono.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    phone.id = 10
    phone.search_text = "veyra habla telefono celular"
    phone.best_for = "escena telefono talking subtitles"
    phone.orientation = "16:9"
    phone.preview_status = "pending"
    phone.ai_enrichment_status = "pending"
    phone.safe_for_subtitles = True

    needs_review = make_selection_asset(
        source=source,
        uid="asset-review",
        filename="veyra_review.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    needs_review.id = 11
    needs_review.search_text = "veyra energia review"
    needs_review.orientation = "9:16"
    needs_review.preview_status = "ready"
    needs_review.ai_enrichment_status = "ready"
    needs_review.ai_enrichment_confidence = 0.4
    needs_review.needs_human_review = True
    needs_review.review_reason = "baja confianza"

    failed_preview = make_selection_asset(
        source=source,
        uid="asset-preview-failed",
        filename="veyra_failed.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    failed_preview.id = 12
    failed_preview.search_text = "veyra energia failed"
    failed_preview.orientation = "9:16"
    failed_preview.preview_status = "failed"
    failed_preview.ai_enrichment_status = "failed"

    image = make_selection_asset(
        source=source,
        uid="asset-image",
        filename="veyra_poster.jpg",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
        asset_type="image",
    )
    image.id = 13
    image.search_text = "veyra energia poster imagen"
    image.orientation = "9:16"
    image.preview_status = "ready"
    image.ai_enrichment_status = "ready"
    image.ai_enrichment_confidence = 0.8

    other = make_selection_asset(
        source=source,
        uid="asset-other-brand",
        filename="otra_marca.mp4",
        brand=other_brand,
        product=other_product,
        usage_scope="brand_exclusive",
    )
    other.search_text = "veyra energia otra marca"

    negative = make_selection_asset(
        source=source,
        uid="asset-negative",
        filename="veyra_triste.mp4",
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
    )
    negative.search_text = "veyra energia triste"
    negative.best_for = "energia triste"
    negative.orientation = "9:16"
    negative.preview_status = "ready"
    negative.ai_enrichment_status = "ready"
    negative.ai_enrichment_confidence = 0.92

    session.add_all(
        [
            mystic,
            duplicate_group,
            phone,
            needs_review,
            failed_preview,
            image,
            other,
            negative,
        ]
    )
    session.commit()
    return {
        "brand": brand,
        "other_brand": other_brand,
        "product": product,
        "mystic": mystic,
        "duplicate_group": duplicate_group,
        "phone": phone,
        "needs_review": needs_review,
        "failed_preview": failed_preview,
        "image": image,
        "other": other,
        "negative": negative,
    }


def make_selection_asset(
    source: Source,
    uid: str,
    filename: str,
    brand: Brand | None,
    product: Product | None,
    usage_scope: str,
    asset_type: str = "video",
) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_grandiosa",
        remote_path=f"Grandiosa Mujer/Veyra/{filename}",
        filename=filename,
        type=asset_type,
        brand=brand,
        product=product,
        status="active",
        usage_scope=usage_scope,
        rights_status="owned",
        auto_select_enabled=True,
        search_text=filename,
        quality_score=1.0,
    )


def select_assets(
    client: TestClient,
    payload: dict[str, object],
    headers: dict[str, str] | None = API_HEADERS,
):
    return client.post("/api/assets/select", json=payload, headers=headers)


def seed_client() -> tuple[TestClient, sessionmaker[Session]]:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_selection_assets(session)
    return client, session_factory


def response_uids(response_json: dict[str, object]) -> list[str]:
    assets = response_json["assets"]
    assert isinstance(assets, list)
    return [asset["asset_uid"] for asset in assets]


def test_select_requires_api_key() -> None:
    client, _ = seed_client()

    response = select_assets(client, {"brand_slug": "grandiosa_mujer"}, headers=None)

    assert response.status_code == 401


def test_select_requires_brand_slug() -> None:
    client, _ = seed_client()

    response = select_assets(client, {"script_scene": "Veyra revela energia"})

    assert response.status_code == 422


def test_select_returns_brand_exclusive_for_correct_brand() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "product_slug": "veyra", "query": "mistica"},
    )

    assert response.status_code == 200
    assert "asset-mystic" in response_uids(response.json())


def test_select_does_not_return_brand_exclusive_for_wrong_brand() -> None:
    client, _ = seed_client()

    response = select_assets(client, {"brand_slug": "otra_marca", "query": "mistica energia"})

    assert response.status_code == 200
    assert "asset-mystic" not in response_uids(response.json())


def test_select_filters_asset_type_video() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "asset_type": "video", "query": "veyra energia"},
    )

    assert response.status_code == 200
    assert all(asset["type"] == "video" for asset in response.json()["assets"])
    assert "asset-image" not in response_uids(response.json())


def test_select_filters_orientation_9_16() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "orientation": "9:16", "query": "veyra"},
    )

    assert response.status_code == 200
    assert all(asset["orientation"] == "9:16" for asset in response.json()["assets"])
    assert "asset-phone" not in response_uids(response.json())


def test_select_require_preview_ready_excludes_pending_and_failed() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "veyra",
            "require_preview_ready": True,
            "count": 20,
        },
    )

    assert response.status_code == 200
    uids = response_uids(response.json())
    assert "asset-phone" not in uids
    assert "asset-preview-failed" not in uids


def test_select_require_ai_ready_excludes_pending_failed_and_needs_review() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "veyra",
            "require_ai_ready": True,
            "count": 20,
        },
    )

    assert response.status_code == 200
    uids = response_uids(response.json())
    assert "asset-phone" not in uids
    assert "asset-preview-failed" not in uids
    assert "asset-review" not in uids


def test_select_allow_needs_review_false_excludes_review_assets() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "review energia",
            "allow_needs_review": False,
            "count": 20,
        },
    )

    assert response.status_code == 200
    assert "asset-review" not in response_uids(response.json())


def test_select_exclude_asset_ids() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "mistica energia",
            "exclude_asset_ids": [8],
            "count": 20,
        },
    )

    assert response.status_code == 200
    assert "asset-mystic" not in response_uids(response.json())


def test_select_negative_keywords_penalize_matches() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "veyra energia",
            "negative_keywords": ["triste"],
            "count": 20,
        },
    )

    assert response.status_code == 200
    uids = response_uids(response.json())
    assert uids.index("asset-negative") > uids.index("asset-mystic")
    negative_asset = next(asset for asset in response.json()["assets"] if asset["asset_uid"] == "asset-negative")
    assert "Penalizado por keyword negativa: triste" in negative_asset["match_reasons"]


def test_select_preferred_keywords_increase_score() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "veyra",
            "preferred_keywords": ["telefono"],
            "count": 20,
        },
    )

    assert response.status_code == 200
    assets = response.json()["assets"]
    phone_asset = next(asset for asset in assets if asset["asset_uid"] == "asset-phone")
    mystic_asset = next(asset for asset in assets if asset["asset_uid"] == "asset-mystic")
    assert phone_asset["score"] > mystic_asset["score"]
    assert "Coincide con keyword: telefono" in phone_asset["match_reasons"]


def test_select_max_per_similarity_group_limits_group() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {
            "brand_slug": "grandiosa_mujer",
            "query": "mistica energia",
            "count": 5,
            "max_per_similarity_group": 1,
        },
    )

    assert response.status_code == 200
    grouped = {"asset-mystic", "asset-mystic-alt"}
    assert len(grouped.intersection(response_uids(response.json()))) == 1


def test_select_response_includes_renderer_metadata() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "query": "mistica energia", "count": 1},
    )

    assert response.status_code == 200
    asset = response.json()["assets"][0]
    assert asset["remote_path"] == "Grandiosa Mujer/Veyra/veyra_mistica_energia.mp4"
    assert asset["rclone_remote"] == "gdrive_grandiosa"
    assert isinstance(asset["score"], float)
    assert asset["match_reasons"]
    assert asset["thumbnail_url"] == "/media/previews/asset-mystic/thumbnail.jpg"
    assert asset["preview_url"] == "/media/previews/asset-mystic/preview.mp4"


def test_select_count_maximum_is_50() -> None:
    client, _ = seed_client()

    accepted = select_assets(client, {"brand_slug": "grandiosa_mujer", "count": 50})
    rejected = select_assets(client, {"brand_slug": "grandiosa_mujer", "count": 51})

    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_select_rejects_include_restricted() -> None:
    client, _ = seed_client()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "include_restricted": True},
    )

    assert response.status_code == 422
