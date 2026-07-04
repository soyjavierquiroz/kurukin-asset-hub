import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def test_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me")
    monkeypatch.setenv("ASSET_HUB_API_KEY", "test-api-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
