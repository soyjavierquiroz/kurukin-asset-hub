from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import JobAssetBundle, JobAssetBundleItem
from tests.test_asset_search_api import API_HEADERS, make_test_client
from tests.test_asset_selection_api import seed_selection_assets


def seed_client() -> tuple[TestClient, sessionmaker[Session]]:
    client, session_factory = make_test_client()
    with session_factory() as session:
        seed_selection_assets(session)
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
