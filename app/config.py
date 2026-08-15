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
    pilot_database_url: str | None = Field(default=None, alias="PILOT_DATABASE_URL")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="change-me", alias="ADMIN_PASSWORD")
    asset_hub_api_key: str = Field(default="change-me", alias="ASSET_HUB_API_KEY")
    preview_storage_dir: str = Field(default="/data/previews", alias="PREVIEW_STORAGE_DIR")
    pilot_preview_root: str = Field(
        default="/var/lib/kurukin-asset-hub-pilot/previews",
        alias="PILOT_PREVIEW_ROOT",
    )
    job_assets_storage_dir: str = Field(
        default="/data/job-assets",
        alias="JOB_ASSETS_STORAGE_DIR",
    )
    job_asset_materialization_enabled: bool = Field(
        default=True,
        alias="JOB_ASSET_MATERIALIZATION_ENABLED",
    )
    asset_editorial_gate_enabled: bool = Field(
        default=False,
        alias="ASSET_EDITORIAL_GATE_ENABLED",
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    nvidia_api_key: str | None = Field(default=None, alias="NVIDIA_API_KEY")
    nvidia_base_url: str = Field(default="https://integrate.api.nvidia.com/v1", alias="NVIDIA_BASE_URL")
    nvidia_model: str = Field(default="nvidia/nemotron-nano-12b-v2-vl", alias="NVIDIA_MODEL")
    nvidia_visual_model: str = Field(
        default="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        alias="NVIDIA_VISUAL_MODEL",
    )
    nvidia_max_tokens: int = Field(default=900, ge=1, alias="NVIDIA_MAX_TOKENS")
    nvidia_max_concurrency: int = Field(default=1, ge=1, alias="NVIDIA_MAX_CONCURRENCY")
    ai_enrichment_enabled: bool = Field(default=False, alias="AI_ENRICHMENT_ENABLED")
    ai_provider: str = Field(default="openai", alias="AI_PROVIDER")
    ai_model: str | None = Field(default=None, alias="AI_MODEL")
    ai_output_language: str = Field(default="es", alias="AI_OUTPUT_LANGUAGE")
    ai_frame_sample_count: int = Field(default=8, ge=1, le=30, alias="AI_FRAME_SAMPLE_COUNT")
    ai_review_threshold: float = Field(default=0.72, ge=0.0, le=1.0, alias="AI_REVIEW_THRESHOLD")
    ai_max_assets_per_batch: int = Field(default=20, ge=1, le=200, alias="AI_MAX_ASSETS_PER_BATCH")
    google_drive_root_folder_id: str | None = Field(default=None, alias="GOOGLE_DRIVE_ROOT_FOLDER_ID")
    google_drive_client: str = Field(default="rclone", alias="GOOGLE_DRIVE_CLIENT")
    google_drive_auth_mode: str = Field(default="adc", alias="GOOGLE_DRIVE_AUTH_MODE")
    google_drive_access_token: str | None = Field(default=None, alias="GOOGLE_DRIVE_ACCESS_TOKEN")
    rclone_remote: str | None = Field(default=None, alias="RCLONE_REMOTE")
    rclone_config: str | None = Field(default=None, alias="RCLONE_CONFIG")
    google_drive_legacy_folder_ids: str | None = Field(default=None, alias="GOOGLE_DRIVE_LEGACY_FOLDER_IDS")
    google_drive_generic_inbox_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID",
    )
    google_drive_brand_inbox_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID",
    )
    google_drive_title_inbox_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID",
    )
    google_drive_generic_library_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_GENERIC_LIBRARY_FOLDER_ID",
    )
    google_drive_brand_library_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_BRAND_LIBRARY_FOLDER_ID",
    )
    google_drive_title_library_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_TITLE_LIBRARY_FOLDER_ID",
    )
    google_drive_review_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_REVIEW_FOLDER_ID",
    )
    google_drive_error_folder_id: str | None = Field(
        default=None,
        alias="GOOGLE_DRIVE_ERROR_FOLDER_ID",
    )
    managed_folder_batch_size: int = Field(default=250, ge=1, alias="MANAGED_FOLDER_BATCH_SIZE")
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
