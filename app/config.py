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
    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
