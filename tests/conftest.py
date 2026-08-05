import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def test_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me")
    monkeypatch.setenv("ASSET_HUB_API_KEY", "test-api-key")
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "root")
    for key in (
        "GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID",
        "GOOGLE_DRIVE_GENERIC_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_BRAND_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_TITLE_LIBRARY_FOLDER_ID",
        "GOOGLE_DRIVE_REVIEW_FOLDER_ID",
        "GOOGLE_DRIVE_ERROR_FOLDER_ID",
    ):
        monkeypatch.setenv(key, f"{key.lower()}-id")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
