from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Asset, Brand, JobAssetBundle, JobAssetBundleItem, Product, Source
from app.schemas.asset_selection import AssetSelectionResponse
from app.services import job_asset_bundles
from tests.test_asset_search_api import API_HEADERS, make_test_client
from tests.test_asset_selection_api import seed_selection_assets


def seed_client() -> tuple[TestClient, sessionmaker[Session]]:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_selection_assets(session)
    return client, session_factory


def seed_mpt_explicit_assets(session: Session) -> dict[str, Asset | Brand | Product]:
    brand = Brand(slug="grandiosa-mujer", name="Grandiosa Mujer")
    product = Product(brand=brand, slug="veyra", name="Veyra")
    source = Source(
        source_id="drive-grandiosa-mpt",
        provider="google_drive",
        label="Grandiosa MPT Drive",
        rclone_remote="gdrive_grandiosa",
    )
    session.add_all([brand, product, source])
    session.flush()

    asset_a = make_mpt_explicit_asset(
        source=source,
        uid="drive-A",
        filename="drive_a.mp4",
        brand=brand,
        product=product,
        scope="brand",
    )
    asset_b = make_mpt_explicit_asset(
        source=source,
        uid="drive-B",
        filename="drive_b.mp4",
        brand=brand,
        product=product,
        scope="brand",
    )
    generic = make_mpt_explicit_asset(
        source=source,
        uid="drive-generic",
        filename="generic.mp4",
        brand=None,
        product=None,
        scope="generic",
    )
    title = make_mpt_explicit_asset(
        source=source,
        uid="drive-title",
        filename="title.mp4",
        brand=None,
        product=None,
        scope="title",
    )
    title.title_name = "Mi Otra Yo"
    title.title_slug = "mi-otra-yo"
    title.title_type = "series"
    restricted = make_mpt_explicit_asset(
        source=source,
        uid="drive-restricted",
        filename="restricted.mp4",
        brand=brand,
        product=product,
        scope="brand",
    )
    restricted.usage_scope = "restricted"
    session.add_all([asset_a, asset_b, generic, title, restricted])
    session.commit()
    return {
        "brand": brand,
        "product": product,
        "asset_a": asset_a,
        "asset_b": asset_b,
        "generic": generic,
        "title": title,
    }


def make_mpt_explicit_asset(
    *,
    source: Source,
    uid: str,
    filename: str,
    brand: Brand | None,
    product: Product | None,
    scope: str,
) -> Asset:
    return Asset(
        asset_uid=uid,
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_grandiosa",
        remote_path=f"Grandiosa Mujer/Veyra/{filename}",
        filename=filename,
        type="video",
        scope=scope,
        brand=brand,
        product=product,
        status="ready",
        move_status="moved",
        source_status="active",
        usage_scope="brand_exclusive",
        rights_status="owned",
        auto_select_enabled=True,
        preview_status="ready",
        ai_enrichment_status="ready",
        orientation="9:16",
        quality_score=1.0,
    )


def seed_mpt_client() -> tuple[TestClient, sessionmaker[Session]]:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_mpt_explicit_assets(session)
    return client, session_factory


def bundle_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": "job-bundle-001",
        "brand_slug": "grandiosa_mujer",
        "product_slug": "veyra",
        "scenes": [
            {
                "scene_id": "scene-001",
                "scene_index": 1,
                "script_scene": "Veyra revela energia mistica mientras mira el celular",
                "asset_type": "video",
                "orientation": "9:16",
                "count": 1,
                "require_preview_ready": True,
            }
        ],
    }
    payload.update(overrides)
    return payload


def mpt_bundle_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": "mpt-001",
        "brand_slug": "grandiosa-mujer",
        "product_slug": "veyra",
        "created_by": "money-printer-turbo",
        "scenes": [
            {
                "scene_id": "scene-001",
                "scene_index": 1,
                "script_scene": "Escena uno",
                "selected_asset_uids": ["drive-A"],
            },
            {
                "scene_id": "scene-002",
                "scene_index": 2,
                "script_scene": "Escena dos",
                "selected_asset_uids": ["drive-B"],
            },
        ],
    }
    payload.update(overrides)
    return payload


def post_bundle(
    client: TestClient,
    payload: dict[str, object],
    headers: dict[str, str] | None = API_HEADERS,
):
    return client.post("/api/jobs/asset-bundles", json=payload, headers=headers)


def test_post_requires_api_key() -> None:
    client, _ = seed_client()

    response = post_bundle(client, bundle_payload(), headers=None)

    assert response.status_code == 401


def test_brand_slug_required() -> None:
    client, _ = seed_client()
    payload = bundle_payload()
    del payload["brand_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 422


def test_create_bundle_with_one_scene_and_asset() -> None:
    client, session_factory = seed_client()

    response = post_bundle(client, bundle_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["total_scenes"] == 1
    assert data["total_assets"] == 1
    assert data["assets"][0]["scene_id"] == "scene-001"
    assert data["assets"][0]["asset_uid"] == "asset-mystic"
    with session_factory() as session:
        bundle = session.scalar(
            select(JobAssetBundle).where(JobAssetBundle.bundle_uid == data["bundle_uid"])
        )
        assert bundle is not None
        assert bundle.job_id == "job-bundle-001"


def test_explicit_bundle_creates_exact_selected_assets() -> None:
    client, session_factory = seed_mpt_client()

    response = post_bundle(client, mpt_bundle_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["brand_slug"] == "grandiosa-mujer"
    assert [(asset["scene_id"], asset["asset_uid"]) for asset in data["assets"]] == [
        ("scene-001", "drive-A"),
        ("scene-002", "drive-B"),
    ]
    with session_factory() as session:
        items = session.scalars(
            select(JobAssetBundleItem)
            .join(JobAssetBundle)
            .where(JobAssetBundle.bundle_uid == data["bundle_uid"])
            .order_by(JobAssetBundleItem.scene_index, JobAssetBundleItem.rank)
        ).all()
        assert [item.asset_uid for item in items] == ["drive-A", "drive-B"]
        assert all(item.asset_id is not None for item in items)


def test_explicit_only_generic_without_brand_slug_is_valid() -> None:
    client, _ = seed_mpt_client()
    payload = mpt_bundle_payload(
        job_id="mpt-generic-no-brand",
        scenes=[
            {
                "scene_id": "scene-001",
                "script_scene": "Escena generic",
                "selected_asset_uids": ["drive-generic"],
            }
        ],
    )
    del payload["brand_slug"]
    del payload["product_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert response.json()["brand_slug"] is None
    assert response.json()["assets"][0]["asset_uid"] == "drive-generic"


def test_explicit_only_title_without_brand_slug_is_valid() -> None:
    client, _ = seed_mpt_client()
    payload = mpt_bundle_payload(
        job_id="mpt-title-no-brand",
        scenes=[
            {
                "scene_id": "scene-001",
                "script_scene": "Escena title",
                "selected_asset_uids": ["drive-title"],
            }
        ],
    )
    del payload["brand_slug"]
    del payload["product_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert response.json()["brand_slug"] is None
    assert response.json()["assets"][0]["asset_uid"] == "drive-title"


def test_explicit_only_brand_asset_without_brand_slug_is_valid() -> None:
    client, _ = seed_mpt_client()
    payload = mpt_bundle_payload(job_id="mpt-brand-asset-no-brand")
    del payload["brand_slug"]
    del payload["product_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert response.json()["brand_slug"] is None
    assert [asset["asset_uid"] for asset in response.json()["assets"]] == ["drive-A", "drive-B"]


def test_auto_selection_without_brand_slug_returns_422() -> None:
    client, _ = seed_client()
    payload = bundle_payload(job_id="job-auto-no-brand")
    del payload["brand_slug"]
    del payload["product_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 422
    assert "brand_slug is required" in str(response.json()["detail"])


def test_mixed_explicit_and_auto_without_brand_slug_returns_422() -> None:
    client, _ = seed_mpt_client()
    payload = mpt_bundle_payload(
        job_id="mpt-mixed-no-brand",
        scenes=[
            {
                "scene_id": "scene-001",
                "script_scene": "Escena explicit",
                "selected_asset_uids": ["drive-A"],
            },
            {
                "scene_id": "scene-002",
                "script_scene": "Escena auto",
            },
        ],
    )
    del payload["brand_slug"]
    del payload["product_slug"]

    response = post_bundle(client, payload)

    assert response.status_code == 422
    assert "brand_slug is required" in str(response.json()["detail"])


def test_explicit_selection_does_not_call_auto_selection(monkeypatch) -> None:
    client, _ = seed_mpt_client()

    def fail_select_assets(*_args, **_kwargs) -> AssetSelectionResponse:
        raise AssertionError("auto-selection should not run for explicit scenes")

    monkeypatch.setattr(job_asset_bundles, "select_assets", fail_select_assets)

    response = post_bundle(client, mpt_bundle_payload(job_id="mpt-no-auto"))

    assert response.status_code == 200
    assert [asset["asset_uid"] for asset in response.json()["assets"]] == ["drive-A", "drive-B"]


def test_explicit_selected_asset_order_preserves_rank() -> None:
    client, session_factory = seed_mpt_client()
    payload = mpt_bundle_payload(
        job_id="mpt-rank",
        scenes=[
            {
                "scene_id": "scene-001",
                "scene_index": 1,
                "script_scene": "Escena uno",
                "selected_asset_uids": ["drive-B", "drive-A"],
            }
        ],
    )

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert [(asset["asset_uid"], asset["rank"]) for asset in response.json()["assets"]] == [
        ("drive-B", 1),
        ("drive-A", 2),
    ]
    with session_factory() as session:
        items = session.scalars(
            select(JobAssetBundleItem)
            .join(JobAssetBundle)
            .where(JobAssetBundle.bundle_uid == response.json()["bundle_uid"])
            .order_by(JobAssetBundleItem.rank)
        ).all()
        assert [(item.asset_uid, item.rank) for item in items] == [("drive-B", 1), ("drive-A", 2)]
        assert [item.score for item in items] == [None, None]
        assert [item.match_reasons for item in items] == [
            ["explicit_selection"],
            ["explicit_selection"],
        ]


def test_explicit_missing_asset_uid_returns_422() -> None:
    client, _ = seed_mpt_client()

    response = post_bundle(
        client,
        mpt_bundle_payload(
            job_id="mpt-missing",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Escena uno",
                    "selected_asset_uids": ["drive-missing"],
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert "drive-missing" in response.json()["detail"]


def test_explicit_restricted_asset_returns_422() -> None:
    client, _ = seed_mpt_client()

    response = post_bundle(
        client,
        mpt_bundle_payload(
            job_id="mpt-restricted",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Escena uno",
                    "selected_asset_uids": ["drive-restricted"],
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert "drive-restricted" in response.json()["detail"]


def test_explicit_selected_and_excluded_same_uid_returns_422() -> None:
    client, _ = seed_mpt_client()

    response = post_bundle(
        client,
        mpt_bundle_payload(
            job_id="mpt-exclude-conflict",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Escena uno",
                    "selected_asset_uids": ["drive-A"],
                    "exclude_asset_uids": ["drive-A"],
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert "drive-A" in str(response.json()["detail"])


def test_explicit_duplicate_within_scene_returns_422() -> None:
    client, _ = seed_mpt_client()

    response = post_bundle(
        client,
        mpt_bundle_payload(
            job_id="mpt-duplicate",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Escena uno",
                    "selected_asset_uids": ["drive-A", "drive-A"],
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert "duplicate" in str(response.json()["detail"])


def test_explicit_same_uid_across_scenes_is_allowed() -> None:
    client, _ = seed_mpt_client()

    response = post_bundle(
        client,
        mpt_bundle_payload(
            job_id="mpt-reuse",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "script_scene": "Escena uno",
                    "selected_asset_uids": ["drive-A"],
                },
                {
                    "scene_id": "scene-002",
                    "scene_index": 2,
                    "script_scene": "Escena dos",
                    "selected_asset_uids": ["drive-A"],
                },
            ],
        ),
    )

    assert response.status_code == 200
    assert [asset["asset_uid"] for asset in response.json()["assets"]] == ["drive-A", "drive-A"]


def test_legacy_bundle_without_explicit_selection_uses_auto_selection(monkeypatch) -> None:
    client, _ = seed_client()
    original_select_assets = job_asset_bundles.select_assets
    calls = 0

    def spy_select_assets(*args, **kwargs) -> AssetSelectionResponse:
        nonlocal calls
        calls += 1
        return original_select_assets(*args, **kwargs)

    monkeypatch.setattr(job_asset_bundles, "select_assets", spy_select_assets)

    response = post_bundle(client, bundle_payload(job_id="job-legacy-auto"))

    assert response.status_code == 200
    assert calls == 1
    assert response.json()["assets"][0]["asset_uid"] == "asset-mystic"


def test_create_bundle_with_multiple_scenes() -> None:
    client, _ = seed_client()
    payload = bundle_payload(
        job_id="job-multi",
        scenes=[
            {
                "scene_id": "scene-001",
                "scene_index": 1,
                "script_scene": "Veyra revela energia mistica",
                "orientation": "9:16",
                "count": 1,
            },
            {
                "scene_id": "scene-002",
                "scene_index": 2,
                "script_scene": "Veyra energia telefono",
                "count": 1,
                "require_preview_ready": False,
            },
        ],
    )

    response = post_bundle(client, payload)

    assert response.status_code == 200
    data = response.json()
    assert data["total_scenes"] == 2
    assert data["total_assets"] == 2
    assert {asset["scene_id"] for asset in data["assets"]} == {"scene-001", "scene-002"}


def test_returns_existing_ready_bundle_when_force_false() -> None:
    client, _ = seed_client()
    first = post_bundle(client, bundle_payload(job_id="job-reuse"))
    assert first.status_code == 200

    second = post_bundle(
        client,
        bundle_payload(
            job_id="job-reuse",
            scenes=[
                {
                    "scene_id": "scene-different",
                    "script_scene": "Veyra habla telefono",
                    "count": 1,
                    "require_preview_ready": False,
                }
            ],
        ),
    )

    assert second.status_code == 200
    assert second.json()["bundle_uid"] == first.json()["bundle_uid"]
    assert second.json()["assets"][0]["scene_id"] == "scene-001"


def test_force_true_creates_new_bundle_and_supersedes_previous() -> None:
    client, session_factory = seed_client()
    first = post_bundle(client, bundle_payload(job_id="job-force"))
    assert first.status_code == 200

    second = post_bundle(client, bundle_payload(job_id="job-force", force=True))

    assert second.status_code == 200
    assert second.json()["bundle_uid"] != first.json()["bundle_uid"]
    with session_factory() as session:
        previous = session.scalar(
            select(JobAssetBundle).where(JobAssetBundle.bundle_uid == first.json()["bundle_uid"])
        )
        assert previous is not None
        assert previous.status == "superseded"


def test_get_by_bundle_uid_works() -> None:
    client, _ = seed_client()
    created = post_bundle(client, bundle_payload(job_id="job-get-uid"))
    assert created.status_code == 200

    response = client.get(
        f"/api/jobs/asset-bundles/{created.json()['bundle_uid']}",
        headers=API_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-get-uid"


def test_get_by_job_id_returns_latest() -> None:
    client, _ = seed_client()
    first = post_bundle(client, bundle_payload(job_id="job-get-latest"))
    assert first.status_code == 200
    second = post_bundle(client, bundle_payload(job_id="job-get-latest", force=True))
    assert second.status_code == 200

    response = client.get("/api/jobs/job-get-latest/asset-bundle", headers=API_HEADERS)

    assert response.status_code == 200
    assert response.json()["bundle_uid"] == second.json()["bundle_uid"]


def test_scene_without_assets_produces_partial_when_other_scenes_have_assets() -> None:
    client, _ = seed_client()
    payload = bundle_payload(
        job_id="job-partial",
        scenes=[
            {
                "scene_id": "scene-hit",
                "script_scene": "Veyra revela energia mistica",
                "orientation": "9:16",
                "count": 1,
            },
            {
                "scene_id": "scene-miss",
                "script_scene": "palabra imposible inexistente",
                "orientation": "square",
                "count": 1,
            },
        ],
    )

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    scenes = response.json()["manifest"]["scenes"]
    assert scenes[1]["selected_count"] == 0


def test_no_scenes_with_assets_produces_failed() -> None:
    client, _ = seed_client()
    payload = bundle_payload(
        job_id="job-failed",
        scenes=[
            {
                "scene_id": "scene-miss",
                "script_scene": "palabra imposible inexistente",
                "orientation": "square",
                "count": 1,
            }
        ],
    )

    response = post_bundle(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["total_assets"] == 0
    assert response.json()["error"] == "No assets selected for any scene"


def test_does_not_repeat_assets_between_scenes() -> None:
    client, _ = seed_client()
    payload = bundle_payload(
        job_id="job-no-repeat",
        scenes=[
            {
                "scene_id": "scene-001",
                "script_scene": "Veyra energia mistica",
                "orientation": "9:16",
                "count": 1,
            },
            {
                "scene_id": "scene-002",
                "script_scene": "Veyra energia mistica",
                "orientation": "9:16",
                "count": 1,
            },
        ],
    )

    response = post_bundle(client, payload)

    assert response.status_code == 200
    asset_ids = [asset["id"] for asset in response.json()["assets"]]
    assert len(asset_ids) == len(set(asset_ids))


def test_global_exclude_asset_ids_works() -> None:
    client, _ = seed_client()

    response = post_bundle(
        client,
        bundle_payload(job_id="job-exclude", global_exclude_asset_ids=[8]),
    )

    assert response.status_code == 200
    assert "asset-mystic" not in [asset["asset_uid"] for asset in response.json()["assets"]]


def test_response_includes_rclone_remote_and_remote_path() -> None:
    client, _ = seed_client()

    response = post_bundle(client, bundle_payload(job_id="job-render-metadata"))

    assert response.status_code == 200
    asset = response.json()["assets"][0]
    assert asset["rclone_remote"] == "gdrive_grandiosa"
    assert asset["remote_path"] == "Grandiosa Mujer/Veyra/veyra_mistica_energia.mp4"


def test_manifest_json_is_saved() -> None:
    client, session_factory = seed_client()
    response = post_bundle(client, bundle_payload(job_id="job-manifest"))
    assert response.status_code == 200

    with session_factory() as session:
        bundle = session.scalar(
            select(JobAssetBundle).where(JobAssetBundle.bundle_uid == response.json()["bundle_uid"])
        )
        assert bundle is not None
        assert bundle.manifest_json["job_id"] == "job-manifest"
        assert bundle.manifest_json["scenes"][0]["assets"][0]["thumbnail_path"]


def test_items_are_saved_with_scene_rank_and_score() -> None:
    client, session_factory = seed_client()
    response = post_bundle(client, bundle_payload(job_id="job-items"))
    assert response.status_code == 200

    with session_factory() as session:
        item = session.scalar(
            select(JobAssetBundleItem)
            .join(JobAssetBundle)
            .where(JobAssetBundle.bundle_uid == response.json()["bundle_uid"])
        )
        assert item is not None
        assert item.scene_id == "scene-001"
        assert item.rank == 1
        assert item.score is not None
        assert item.match_reasons


def test_does_not_return_brand_exclusive_asset_for_wrong_brand() -> None:
    client, _ = seed_client()

    response = post_bundle(
        client,
        bundle_payload(
            job_id="job-wrong-brand",
            brand_slug="otra_marca",
            product_slug=None,
            scenes=[{"scene_id": "scene-001", "script_scene": "veyra energia", "count": 10}],
        ),
    )

    assert response.status_code == 200
    assert "asset-mystic" not in [asset["asset_uid"] for asset in response.json()["assets"]]


def test_respects_allow_needs_review_false() -> None:
    client, _ = seed_client()

    response = post_bundle(
        client,
        bundle_payload(
            job_id="job-no-review",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Veyra energia review",
                    "orientation": "9:16",
                    "count": 10,
                    "allow_needs_review": False,
                }
            ],
        ),
    )

    assert response.status_code == 200
    assert "asset-review" not in [asset["asset_uid"] for asset in response.json()["assets"]]


def test_respects_require_preview_ready_true() -> None:
    client, _ = seed_client()

    response = post_bundle(
        client,
        bundle_payload(
            job_id="job-preview-ready",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "script_scene": "Veyra energia telefono",
                    "count": 10,
                    "require_preview_ready": True,
                }
            ],
        ),
    )

    assert response.status_code == 200
    assets = response.json()["assets"]
    assert assets
    assert "asset-phone" not in [asset["asset_uid"] for asset in assets]
    assert all(asset["preview_status"] == "ready" for asset in assets)
