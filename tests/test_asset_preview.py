from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Asset, Source
from app.services import asset_preview
from app.services.asset_preview import (
    generate_asset_preview,
    generate_video_thumbnail,
    infer_orientation,
    parse_ffprobe_metadata,
    relative_preview_path,
    relative_thumbnail_path_for_asset,
    thumbnail_timestamp,
)
from app.services.rclone_service import RcloneError, RcloneService


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


def make_asset(session: Session, asset_type: str = "video") -> Asset:
    source = Source(
        source_id="gdrive_code_x",
        provider="google_drive",
        label="Drive Code X",
        rclone_remote="gdrive_code_x",
        root_path="assets",
    )
    asset = Asset(
        asset_uid="asset_test_001",
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_code_x",
        remote_path="assets/video.mp4",
        filename="video.mp4",
        type=asset_type,
    )
    session.add(asset)
    session.commit()
    return asset


def test_infer_orientation_values() -> None:
    assert infer_orientation(1080, 1920) == "9:16"
    assert infer_orientation(1920, 1080) == "16:9"
    assert infer_orientation(1200, 1200) == "square"
    assert infer_orientation(None, 1080) == "unknown"


def test_parse_ffprobe_metadata() -> None:
    metadata = parse_ffprobe_metadata(
        {
            "format": {"duration": "12.34"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30000/1001",
                    "codec_name": "h264",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )

    assert metadata.duration_seconds == 12.34
    assert metadata.width == 1080
    assert metadata.height == 1920
    assert metadata.fps == pytest.approx(29.97, rel=0.001)
    assert metadata.codec == "h264"
    assert metadata.has_audio is True


def test_relative_preview_path_uses_safe_asset_uid() -> None:
    assert (
        relative_preview_path("../asset uid?", "thumbnail.jpg")
        == "previews/asset_uid/thumbnail.jpg"
    )


def test_relative_thumbnail_path_for_asset_uses_public_asset_path() -> None:
    assert relative_thumbnail_path_for_asset(123) == "assets/previews/123/thumbnail.jpg"


def test_thumbnail_timestamp_avoids_first_frame() -> None:
    assert thumbnail_timestamp(10) == pytest.approx(3.5)
    assert thumbnail_timestamp(2) == pytest.approx(1.0)
    assert thumbnail_timestamp(0) == pytest.approx(0.5)


def test_generate_video_thumbnail_uses_jpg_temp_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "nested" / "thumbnail.jpg"
    stale_temp = output_path.with_name(".thumbnail.tmp.jpg")
    output_path.parent.mkdir(parents=True)
    stale_temp.write_bytes(b"stale")
    ffmpeg_output_paths: list[Path] = []

    def fake_run_ffmpeg(command: list[str], operation: str, timeout: int = 300) -> None:
        assert operation == "video thumbnail"
        ffmpeg_output = Path(command[-1])
        ffmpeg_output_paths.append(ffmpeg_output)
        assert ffmpeg_output.name == ".thumbnail.tmp.jpg"
        assert ffmpeg_output.suffix == ".jpg"
        ffmpeg_output.write_bytes(b"\xff\xd8jpeg\xff\xd9")

    monkeypatch.setattr(asset_preview, "run_ffmpeg", fake_run_ffmpeg)

    generate_video_thumbnail(tmp_path / "input.mp4", output_path, duration_seconds=10)

    assert ffmpeg_output_paths == [stale_temp]
    assert output_path.read_bytes() == b"\xff\xd8jpeg\xff\xd9"
    assert not stale_temp.exists()


def test_generate_asset_preview_success(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PREVIEW_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    asset = make_asset(session)

    def fake_copyto(self: RcloneService, remote: str, remote_path: str, local_path: str) -> None:
        assert remote == "gdrive_code_x"
        assert remote_path == "assets/video.mp4"
        Path(local_path).write_bytes(b"video")

    def fake_thumbnail(
        input_path: Path,
        output_path: Path,
        duration_seconds: float | None = None,
    ) -> None:
        output_path.write_bytes(b"thumbnail")

    def fake_preview(input_path: Path, output_path: Path) -> None:
        output_path.write_bytes(b"preview")

    monkeypatch.setattr(RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        asset_preview,
        "run_ffprobe",
        lambda path: {
            "format": {"duration": "3.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30/1",
                    "codec_name": "h264",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        },
    )
    monkeypatch.setattr(asset_preview, "generate_video_thumbnail", fake_thumbnail)
    monkeypatch.setattr(asset_preview, "generate_video_preview", fake_preview)

    result = generate_asset_preview(session, asset.id)

    assert result.preview_status == "ready"
    assert result.technical_metadata_status == "ready"
    assert result.duration_seconds == 3.5
    assert result.width == 1080
    assert result.height == 1920
    assert result.fps == 30
    assert result.codec == "h264"
    assert result.has_audio is True
    assert result.orientation == "9:16"
    assert result.thumbnail_path == f"assets/previews/{asset.id}/thumbnail.jpg"
    assert result.preview_path == f"assets/previews/{asset.id}/preview.mp4"


def test_generate_image_preview_uses_public_asset_storage(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PREVIEW_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    asset = make_asset(session, asset_type="image")

    def fake_copyto(self: RcloneService, remote: str, remote_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"image")

    def fake_image_preview(input_path: Path, thumbnail_path: Path, preview_path: Path) -> None:
        thumbnail_path.write_bytes(b"thumbnail")
        preview_path.write_bytes(b"preview")

    monkeypatch.setattr(RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(asset_preview, "run_ffprobe", lambda path: {"format": {}, "streams": []})
    monkeypatch.setattr(asset_preview, "generate_image_preview", fake_image_preview)

    result = generate_asset_preview(session, asset.id)

    assert result.preview_status == "ready"
    assert result.thumbnail_path == f"assets/previews/{asset.id}/thumbnail.jpg"
    assert result.preview_path == f"assets/previews/{asset.id}/preview.jpg"
    assert (tmp_path / "assets" / "previews" / str(asset.id) / "thumbnail.jpg").is_file()
    assert (tmp_path / "assets" / "previews" / str(asset.id) / "preview.jpg").is_file()


def test_generate_asset_preview_rclone_failure(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PREVIEW_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    asset = make_asset(session)

    def fake_copyto(self: RcloneService, remote: str, remote_path: str, local_path: str) -> None:
        raise RcloneError("rclone copyto failed: token = secret-value")

    monkeypatch.setattr(RcloneService, "copyto", fake_copyto)

    result = generate_asset_preview(session, asset.id)

    assert result.preview_status == "failed"
    assert result.technical_metadata_status == "failed"
    assert result.preview_error is not None
    assert "secret-value" not in result.preview_error
    assert "[redacted]" in result.preview_error
