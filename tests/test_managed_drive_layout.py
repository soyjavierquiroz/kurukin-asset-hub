from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import managed_drive_pilot
from app.services.managed_drive import layout
from app.services.managed_drive_pilot import Classification, IngestRequest


MOVED_FUNCTIONS = {
    "compact_folder_primary_topic",
    "folder_context_slug",
    "legacy_context_slug",
    "route_parts",
}


def request_for(scope: str, **kwargs) -> IngestRequest:
    data = {
        "source_folder_id": "inbox",
        "destination_root_id": "root",
        "scope": scope,
        "limit": 10,
        "dry_run": True,
        "apply": False,
    }
    data.update(kwargs)
    return IngestRequest(**data)


def test_layout_helpers_are_reexported_from_managed_drive_pilot() -> None:
    assert managed_drive_pilot.compact_folder_primary_topic is layout.compact_folder_primary_topic
    assert managed_drive_pilot.folder_context_slug is layout.folder_context_slug
    assert managed_drive_pilot.legacy_context_slug is layout.legacy_context_slug
    assert managed_drive_pilot.route_parts is layout.route_parts


def test_moved_helpers_are_not_duplicated_in_facade() -> None:
    pilot_tree = ast.parse(Path(managed_drive_pilot.__file__).read_text())
    layout_tree = ast.parse(Path(layout.__file__).read_text())

    pilot_defs = {node.name for node in ast.walk(pilot_tree) if isinstance(node, ast.FunctionDef)}
    layout_defs = {node.name for node in ast.walk(layout_tree) if isinstance(node, ast.FunctionDef)}

    assert MOVED_FUNCTIONS.isdisjoint(pilot_defs)
    assert MOVED_FUNCTIONS.issubset(layout_defs)


def test_route_parts_keeps_compact_v2_generic_brand_title_and_review_paths() -> None:
    generic = layout.route_parts(
        request_for("generic"),
        None,
        "video",
        Classification("personas", "bienestar yoga"),
        "vertical-9x16",
    )
    brand = layout.route_parts(
        request_for("brand", brand_slug="grandiosa-mujer", collection="evergreen"),
        None,
        "video",
        Classification("personas", "bienestar yoga"),
        "vertical-9x16",
        batch_number=2,
    )
    title = layout.route_parts(
        request_for("title", title_type="series", title="Mi otra yo", season=1, episode=3),
        None,
        "video",
        Classification("personas", "bienestar yoga"),
        "vertical-9x16",
    )
    review = layout.route_parts(
        request_for("title", title_type="movie", title="Rocky"),
        None,
        "video",
        Classification("otros", "otros", ambiguous=True),
        "horizontal-16x9",
    )

    assert generic == ["10_genericos", "video", "lote-0001"]
    assert brand == ["20_marcas", "grandiosa-mujer", "evergreen", "video", "lote-0002"]
    assert title == [
        "30_peliculas_series",
        "series",
        "mi-otra-yo",
        "temporada-01",
        "episodio-03",
        "video",
        "lote-0001",
    ]
    assert review == ["90_revision", "title", "rocky", "video", "lote-0001"]


def test_context_and_slugs_keep_existing_precedence() -> None:
    brand = SimpleNamespace(slug="brand-from-row")
    asset_brand = SimpleNamespace(slug="brand-from-asset")
    asset = SimpleNamespace(brand=asset_brand, title_slug="title-from-asset")

    assert layout.folder_context_slug(request_for("generic")) is None
    assert layout.folder_context_slug(request_for("brand", brand_slug="Brand From Request"), brand=brand) == "brand-from-row"
    assert layout.folder_context_slug(request_for("brand", brand_slug="Brand From Request"), asset=asset) == "brand-from-asset"
    assert layout.folder_context_slug(request_for("brand", brand_slug="Brand From Request")) == "brand-from-request"
    assert layout.folder_context_slug(request_for("title", title="Mi otra yo"), asset=asset) == "title-from-asset"
    assert layout.folder_context_slug(request_for("title", title="Mi otra yo")) == "mi-otra-yo"


def test_legacy_context_slug_keeps_existing_values() -> None:
    brand = SimpleNamespace(slug="grandiosa-mujer")

    assert layout.legacy_context_slug(SimpleNamespace(scope="generic", brand=None, title_slug=None, title_name=None)) is None
    assert (
        layout.legacy_context_slug(SimpleNamespace(scope="brand", brand=brand, title_slug=None, title_name=None))
        == "grandiosa-mujer"
    )
    assert layout.legacy_context_slug(SimpleNamespace(scope="brand", brand=None, title_slug=None, title_name=None)) is None
    assert (
        layout.legacy_context_slug(SimpleNamespace(scope="title", brand=None, title_slug="rocky", title_name="Rocky IV"))
        == "rocky"
    )
    assert (
        layout.legacy_context_slug(SimpleNamespace(scope="title", brand=None, title_slug=None, title_name="Rocky IV"))
        == "rocky-iv"
    )


def test_compact_folder_primary_topic_keeps_review_boundary() -> None:
    assert layout.compact_folder_primary_topic(SimpleNamespace(status="move_planned", needs_human_review=False)) == "compact"
    assert (
        layout.compact_folder_primary_topic(SimpleNamespace(status="review_required", needs_human_review=False))
        == "compact_review"
    )
    assert layout.compact_folder_primary_topic(SimpleNamespace(status="move_planned", needs_human_review=True)) == "compact_review"


def test_route_parts_keeps_existing_error_for_non_numeric_episode_values() -> None:
    with pytest.raises(ValueError):
        layout.route_parts(
            request_for("title", title_type="series", title="Mi otra yo", season="one", episode=3),
            None,
            "video",
            Classification("personas", "bienestar yoga"),
            "vertical-9x16",
        )
