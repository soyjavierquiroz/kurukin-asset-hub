from __future__ import annotations

import sys

from app.models import Asset, Brand, BrandAssetPolicy, Source
from scripts.sync_rclone_source import main as sync_cli_main
from tests.test_asset_search_api import get_asset_uids, make_asset, make_test_client
from tests.test_asset_selection_api import response_uids, select_assets


def test_source_detail_shows_scan_button() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        source = Source(
            source_id="drive_grandiosa_mujer_veyra",
            provider="google_drive",
            label="Veyra",
            rclone_remote="drive",
            root_path="Grandiosa/Veyra",
        )
        session.add(source)
        session.commit()
        source_id = source.id

    response = client.get("/sources/%s" % source_id, auth=("admin", "change-me"))

    assert response.status_code == 200
    assert "Scan source" in response.text
    assert "Apply sync" in response.text


def test_missing_asset_does_not_appear_in_search() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        source = Source(source_id="drive-main", provider="google_drive", label="Drive Main")
        session.add(source)
        session.flush()
        asset = make_asset(
            source=source,
            uid="asset-missing",
            filename="missing_mistica.mp4",
            brand=None,
            product=None,
            usage_scope="global",
        )
        asset.source_status = "missing"
        session.add(asset)
        session.commit()

    assert "asset-missing" not in get_asset_uids(client, {"q": "missing_mistica"})


def test_missing_asset_does_not_appear_in_select() -> None:
    client, session_factory = make_test_client()
    with session_factory() as session:
        source = Source(source_id="drive-main", provider="google_drive", label="Drive Main")
        brand = Brand(slug="grandiosa_mujer", name="Grandiosa Mujer")
        brand.asset_policy = BrandAssetPolicy(allow_global_video=True)
        session.add_all([source, brand])
        session.flush()
        asset = Asset(
            asset_uid="asset-missing",
            source=source,
            provider="google_drive",
            remote_path="/assets/missing.mp4",
            filename="missing.mp4",
            type="video",
            brand=None,
            product=None,
            status="active",
            source_status="missing",
            usage_scope="global",
            rights_status="owned",
            auto_select_enabled=True,
            search_text="missing veyra energia",
        )
        session.add(asset)
        session.commit()

    response = select_assets(
        client,
        {"brand_slug": "grandiosa_mujer", "query": "missing", "count": 20},
    )
    assert response.status_code == 200
    assert "asset-missing" not in response_uids(response.json())


def test_cli_dry_run_prints_summary(monkeypatch, capsys) -> None:
    client, session_factory = make_test_client()
    del client
    with session_factory() as session:
        source = Source(
            source_id="drive_grandiosa_mujer_veyra",
            provider="google_drive",
            label="Veyra",
            rclone_remote="drive",
            root_path="Grandiosa/Veyra",
        )
        session.add(source)
        session.commit()

    monkeypatch.setattr("scripts.sync_rclone_source.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.services.source_sync.RcloneService.list_json",
        lambda *_: [
            {
                "Path": "clip.mp4",
                "Name": "clip.mp4",
                "Size": 100,
                "ModTime": "2026-07-20T10:00:00Z",
                "ID": "file-1",
                "MimeType": "video/mp4",
            }
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_rclone_source.py", "--source-id", "drive_grandiosa_mujer_veyra", "--dry-run"],
    )

    sync_cli_main()

    output = capsys.readouterr().out
    assert "total_remote=1" in output
    assert "new=1" in output
    assert "run_uid=" in output
