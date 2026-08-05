from app.services.people_metadata import (
    gendered_metadata_allowed,
    people_search_terms,
    source_presentation_hint,
)


def test_people_search_terms_apply_required_gendered_terms() -> None:
    assert {"persona", "hombre"}.issubset(people_search_terms("masculine"))
    assert {"persona", "mujer"}.issubset(people_search_terms("feminine"))
    assert {"personas", "hombres", "mujeres", "grupo"}.issubset(
        people_search_terms("mixed")
    )


def test_people_search_terms_unclear_removes_gendered_terms() -> None:
    terms = people_search_terms("unclear", "silhouette", ["hombre", "mujer"])

    assert "persona" in terms
    assert "silueta" in terms
    assert "hombre" not in terms
    assert "mujer" not in terms


def test_source_presentation_hint_is_filename_only_hint() -> None:
    assert source_presentation_hint("clip_a_man_walking.mp4") == "masculine"
    assert source_presentation_hint("clip_a_woman_walking.mp4") == "feminine"
    assert source_presentation_hint("clip_person_walking.mp4") == "unclear"


def test_gendered_metadata_requires_confidence_and_visibility() -> None:
    assert gendered_metadata_allowed(0.8, "clear")
    assert gendered_metadata_allowed(0.92, "partial")
    assert not gendered_metadata_allowed(0.79, "clear")
    assert not gendered_metadata_allowed(0.95, "silhouette")
