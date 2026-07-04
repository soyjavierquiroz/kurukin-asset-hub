from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    asset_hub_host: str = Field(default="assets.kuruk.in", alias="ASSET_HUB_HOST")
    stack_name: str = Field(default="kurukin-asset-hub", alias="STACK_NAME")
    postgres_db: str = Field(default="kurukin_asset_hub", alias="POSTGRES_DB")
    postgres_user: str = Field(default="asset_hub", alias="POSTGRES_USER")
    postgres_password: str = Field(default="change-me", alias="POSTGRES_PASSWORD")
    database_url: str = Field(
        default="postgresql+psycopg://asset_hub:change-me@db:5432/kurukin_asset_hub",
        alias="DATABASE_URL",
    )
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="change-me", alias="ADMIN_PASSWORD")
    asset_hub_api_key: str = Field(default="change-me", alias="ASSET_HUB_API_KEY")
    preview_storage_dir: str = Field(default="/data/previews", alias="PREVIEW_STORAGE_DIR")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    ai_enrichment_enabled: bool = Field(default=False, alias="AI_ENRICHMENT_ENABLED")
    ai_provider: str = Field(default="openai", alias="AI_PROVIDER")
    ai_model: str | None = Field(default=None, alias="AI_MODEL")
    ai_output_language: str = Field(default="es", alias="AI_OUTPUT_LANGUAGE")
    ai_frame_sample_count: int = Field(default=8, ge=1, le=30, alias="AI_FRAME_SAMPLE_COUNT")
    ai_review_threshold: float = Field(default=0.72, ge=0.0, le=1.0, alias="AI_REVIEW_THRESHOLD")
    ai_max_assets_per_batch: int = Field(default=20, ge=1, le=200, alias="AI_MAX_ASSETS_PER_BATCH")
    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
