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
    job_assets_storage_dir: str = Field(
        default="/data/job-assets",
        alias="JOB_ASSETS_STORAGE_DIR",
    )
    job_asset_materialization_enabled: bool = Field(
        default=True,
        alias="JOB_ASSET_MATERIALIZATION_ENABLED",
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    nvidia_api_key: str | None = Field(default=None, alias="NVIDIA_API_KEY")
    ai_enrichment_enabled: bool = Field(default=False, alias="AI_ENRICHMENT_ENABLED")
    ai_provider: str = Field(default="openai", alias="AI_PROVIDER")
    ai_model: str | None = Field(default=None, alias="AI_MODEL")
    ai_output_language: str = Field(default="es", alias="AI_OUTPUT_LANGUAGE")
    ai_frame_sample_count: int = Field(default=8, ge=1, le=30, alias="AI_FRAME_SAMPLE_COUNT")
    ai_review_threshold: float = Field(default=0.72, ge=0.0, le=1.0, alias="AI_REVIEW_THRESHOLD")
    ai_max_assets_per_batch: int = Field(default=20, ge=1, le=200, alias="AI_MAX_ASSETS_PER_BATCH")
    asset_pipeline_enabled: bool = Field(default=True, alias="ASSET_PIPELINE_ENABLED")
    asset_pipeline_poll_seconds: float = Field(
        default=30.0,
        ge=1.0,
        alias="ASSET_PIPELINE_POLL_SECONDS",
    )
    asset_pipeline_batch_size: int = Field(
        default=10,
        ge=1,
        le=100,
        alias="ASSET_PIPELINE_BATCH_SIZE",
    )
    long_video_segmentation_enabled: bool = Field(
        default=True,
        alias="LONG_VIDEO_SEGMENTATION_ENABLED",
    )
    long_video_threshold_seconds: float = Field(default=30.0, alias="LONG_VIDEO_THRESHOLD_SECONDS")
    segment_min_seconds: float = Field(default=4.0, alias="SEGMENT_MIN_SECONDS")
    segment_target_seconds: float = Field(default=8.0, alias="SEGMENT_TARGET_SECONDS")
    segment_max_seconds: float = Field(default=15.0, alias="SEGMENT_MAX_SECONDS")
    segment_scene_threshold: float = Field(default=0.32, alias="SEGMENT_SCENE_THRESHOLD")
    segment_output_mode: str = Field(
        default="high_quality_encode",
        alias="SEGMENT_OUTPUT_MODE",
    )
    segment_output_crf: int = Field(default=17, alias="SEGMENT_OUTPUT_CRF")
    segment_output_preset: str = Field(default="medium", alias="SEGMENT_OUTPUT_PRESET")
    segment_strip_audio: bool = Field(default=True, alias="SEGMENT_STRIP_AUDIO")
    derived_asset_category_folders: str = Field(
        default="women,men,couples,animals,family,business,nature,food,health,abstract,spiritual,other",
        alias="DERIVED_ASSET_CATEGORY_FOLDERS",
    )
    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
