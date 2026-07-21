from __future__ import annotations

from datetime import UTC

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Asset, Brand, JobAssetBundleItem, Product, Source
from app.services import long_video_segmentation as svc
from app.services.asset_selection import eligible_selection_base_filter
from app.services.renderer_manifest import build_renderer_asset_entry
from scripts.segment_long_videos import parse_args


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


def seed_parent(session: Session, duration: float = 90.0) -> Asset:
    brand = Brand(slug="kurukin", name="Kurukin")
    product = Product(brand=brand, slug="veyra", name="Veyra")
    source = Source(
        source_id="drive_stock_raw_long",
        provider="google_drive",
        label="Raw long",
        rclone_remote="gdrive_raw",
        root_path="Assets/stock/raw-long/inbox",
        source_role="raw_long",
        derived_rclone_remote="gdrive_stock_derived_brolls",
        derived_root_path="legacy/nested-root",
    )
    asset = Asset(
        asset_uid="asset-parent-001",
        source=source,
        provider="google_drive",
        rclone_remote="gdrive_raw",
        remote_path="Assets/stock/raw-long/inbox/Mamá Ánimo.mp4",
        filename="Mamá Ánimo.mp4",
        type="video",
        duration_seconds=duration,
        brand=brand,
        product=product,
        usage_scope="brand_exclusive",
        rights_status="owned",
        auto_select_enabled=True,
        source_status="active",
        has_audio=True,
    )
    session.add(asset)
    session.commit()
    return asset


def patch_successful_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.RcloneService, "copyto", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "export_segment", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "extract_segment_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "generate_child_enrichment", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        svc,
        "analyze_segment_for_naming",
        lambda local_clip_path, local_thumbnail_path, parent_asset, plan=None: svc.suggest_segment_title_and_filename(
            parent_asset,
            plan or svc.SegmentPlan(1, 0, 0),
        ),
    )
    monkeypatch.setattr(
        svc,
        "detect_video_segments",
        lambda *args, **kwargs: [
            svc.SegmentPlan(index=1, start_seconds=0, end_seconds=8),
            svc.SegmentPlan(index=2, start_seconds=8, end_seconds=16),
        ],
    )


def test_video_below_threshold_is_skipped(session: Session) -> None:
    parent = seed_parent(session, duration=12.0)

    run = svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")

    assert run.status == "ready"
    assert parent.segmentation_status == "skipped"
    assert parent.segment_count == 0
    assert parent.auto_select_enabled is False


def test_detect_fallback_windows_produce_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "probe_duration", lambda path: 26.0)
    monkeypatch.setattr(svc, "detect_scene_segments", lambda *args, **kwargs: [])

    plans = svc.detect_video_segments(__file__, svc.segmentation_config())

    assert [(plan.start_seconds, plan.end_seconds) for plan in plans] == [
        (0.0, 8.0),
        (8.0, 16.0),
        (16.0, 24.0),
    ]


def test_safe_filename_removes_accents_and_path_traversal() -> None:
    assert svc.safe_segment_filename("../Mamá Ánimo", 1) == "mama_animo_segment_001.mp4"


def test_build_derived_remote_path_empty_root() -> None:
    assert svc.build_derived_remote_path("", "couples", "test.mp4") == "couples/test.mp4"


def test_build_derived_remote_path_none_root() -> None:
    assert svc.build_derived_remote_path(None, "family", "test.mp4") == "family/test.mp4"


def test_build_derived_remote_path_nested_root() -> None:
    assert svc.build_derived_remote_path("nested/root", "couples", "test.mp4") == "nested/root/couples/test.mp4"


def test_build_derived_remote_path_rejects_path_traversal() -> None:
    with pytest.raises(svc.LongVideoSegmentationError):
        svc.build_derived_remote_path("../root", "couples", "test_segment_001.mp4")
    with pytest.raises(svc.LongVideoSegmentationError):
        svc.build_derived_remote_path("", "couples", "../test_segment_001.mp4")


def test_export_segment_strips_audio_when_enabled() -> None:
    command = svc.build_export_segment_command(
        input_path=__file__,
        output_path=__file__,
        start=1,
        end=9,
        config={
            "output_mode": "high_quality_encode",
            "output_crf": 17,
            "output_preset": "medium",
            "strip_audio": True,
        },
    )

    assert "-an" in command
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "-c:a" not in command


def test_export_segment_stream_copy_is_video_only_when_enabled() -> None:
    command = svc.build_export_segment_command(
        input_path=__file__,
        output_path=__file__,
        start=1,
        end=9,
        config={
            "output_mode": "stream_copy",
            "output_crf": 17,
            "output_preset": "medium",
            "strip_audio": True,
        },
    )

    assert "-an" in command
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-c:v") + 1] == "copy"
    assert "-c:a" not in command


def test_child_asset_inherits_policy_and_starts_pending(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)

    run = svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")
    children = session.scalars(select(Asset).where(Asset.parent_asset_id == parent.id)).all()

    assert run.status == "ready"
    assert len(children) == 2
    assert children[0].brand_id == parent.brand_id
    assert children[0].product_id == parent.product_id
    assert children[0].usage_scope == parent.usage_scope
    assert children[0].rights_status == parent.rights_status
    assert children[0].preview_status == "pending"
    assert children[0].technical_metadata_status == "pending"
    assert children[0].ai_enrichment_status == "pending"
    assert children[0].has_audio is False
    assert children[0].auto_select_enabled is True
    assert parent.has_audio is True
    assert parent.auto_select_enabled is False
    assert parent.delete_original_eligible is True


def test_ai_valid_category_is_used_in_remote_path(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    uploaded = []
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda local, remote, path: uploaded.append(path))
    monkeypatch.setattr(
        svc,
        "analyze_segment_for_naming",
        lambda local_clip_path, local_thumbnail_path, parent_asset, plan=None: svc.SegmentNaming(
            category="women",
            title="Mujer meditando",
            filename_slug="mujer_meditando",
            keywords=["mujer", "meditacion"],
            confidence=0.91,
            needs_review=False,
        ),
    )

    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="Assets/derived")
    child = session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id))
    segment = session.scalar(select(svc.AssetSegment).where(svc.AssetSegment.parent_asset_id == parent.id))

    assert uploaded[0] == "Assets/derived/women/mujer_meditando_segment_001.mp4"
    assert child is not None
    assert child.remote_path == uploaded[0]
    assert segment is not None
    assert segment.remote_path == child.remote_path
    assert child.title == "Mujer meditando"
    assert "meditacion" in (child.search_text or "")
    assert "women" in child.remote_path


def test_upload_path_matches_child_and_segment_remote_path_when_root_empty(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    uploaded = []
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda local, remote, path: uploaded.append(path))
    monkeypatch.setattr(
        svc,
        "detect_video_segments",
        lambda *args, **kwargs: [svc.SegmentPlan(index=1, start_seconds=0, end_seconds=8)],
    )
    monkeypatch.setattr(
        svc,
        "analyze_segment_for_naming",
        lambda local_clip_path, local_thumbnail_path, parent_asset, plan=None: svc.SegmentNaming(
            category="couples",
            title="Couple test",
            filename_slug="test",
            keywords=[],
            confidence=0.91,
            needs_review=False,
        ),
    )

    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")
    child = session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id))
    segment = session.scalar(select(svc.AssetSegment).where(svc.AssetSegment.parent_asset_id == parent.id))

    assert uploaded == ["couples/test_segment_001.mp4"]
    assert child is not None
    assert child.remote_path == uploaded[0]
    assert segment is not None
    assert segment.remote_path == child.remote_path


def test_cli_empty_derived_root_overrides_source_default(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    uploaded = []
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda local, remote, path: uploaded.append(path))
    monkeypatch.setattr(
        svc,
        "detect_video_segments",
        lambda *args, **kwargs: [svc.SegmentPlan(index=1, start_seconds=0, end_seconds=8)],
    )

    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")

    assert uploaded == ["other/mama_animo_segment_001.mp4"]
    assert "Assets/stock/derived-brolls" not in uploaded[0]


def test_parse_args_preserves_empty_derived_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["segment_long_videos.py", "--asset-id", "20", "--derived-root", ""])

    args = parse_args()

    assert args.derived_root == ""


def test_invalid_ai_category_falls_back_to_other(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    uploaded = []
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda local, remote, path: uploaded.append(path))
    monkeypatch.setattr(
        svc,
        "analyze_segment_for_naming",
        lambda local_clip_path, local_thumbnail_path, parent_asset, plan=None: svc.SegmentNaming(
            category="nature",
            title="Bosque raro",
            filename_slug="bosque_raro",
            keywords=[],
            confidence=0.95,
            needs_review=False,
        ),
    )

    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="/")

    assert uploaded[0] == "other/mama_animo_segment_001.mp4"


def test_ai_filename_slug_is_sanitized(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(
        svc,
        "analyze_segment_for_naming",
        lambda local_clip_path, local_thumbnail_path, parent_asset, plan=None: svc.SegmentNaming(
            category="business",
            title="Equipo trabajando",
            filename_slug="../Equipo Ánimo!! segment_001",
            keywords=["equipo"],
            confidence=0.88,
            needs_review=False,
        ),
    )

    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="/")
    child = session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id))

    assert child is not None
    assert child.filename == "equipo_animo_segment_001.mp4"
    assert child.remote_path == "business/equipo_animo_segment_001.mp4"


def test_ai_failure_falls_back_to_other(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    uploaded = []
    patch_successful_pipeline(monkeypatch)
    monkeypatch.setattr(svc, "upload_segment_to_drive", lambda local, remote, path: uploaded.append(path))

    def fail_naming(*args, **kwargs):
        raise RuntimeError("AI down")

    monkeypatch.setattr(svc, "analyze_segment_for_naming", fail_naming)

    run = svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="/")

    assert run.status == "ready"
    assert uploaded[0] == "other/mama_animo_segment_001.mp4"


def test_renderer_manifest_preserves_derivative_has_audio_false(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")
    child = session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id))
    assert child is not None
    item = JobAssetBundleItem(
        scene_id="scene-1",
        scene_index=1,
        asset=child,
        asset_uid=child.asset_uid,
        rank=1,
        selection_json={},
    )

    entry = build_renderer_asset_entry(item, child)

    assert entry["has_audio"] is False
    assert not any("audio" in warning.lower() for warning in entry["render_warnings"])


def test_child_preview_uses_stored_remote_path(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")
    child = session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id))
    copied_paths = []

    def fake_copyto(self, remote, remote_path, local_path):
        copied_paths.append(remote_path)

    monkeypatch.setattr(svc.RcloneService, "copyto", fake_copyto)
    monkeypatch.setattr(
        "app.services.asset_preview.run_ffprobe",
        lambda path: {
            "format": {"duration": "8.0"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}],
        },
    )
    monkeypatch.setattr("app.services.asset_preview.generate_video_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.asset_preview.generate_video_preview", lambda *args, **kwargs: None)

    assert child is not None
    svc.generate_asset_preview(session, child.id, force=True)

    assert copied_paths == [child.remote_path]


def test_delete_original_requires_approval(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")

    with pytest.raises(svc.LongVideoSegmentationError):
        svc.delete_original_after_approval(session, parent.id)

    assert parent.original_delete_status == "skipped"


def test_approve_segmentation_sets_timestamp(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")

    svc.approve_segmentation(session, parent.id, approved_by="admin")

    assert parent.segmentation_approved_at is not None
    assert parent.segmentation_approved_at.tzinfo is not None or UTC is not None
    assert parent.segmentation_approved_by == "admin"


def test_delete_original_approved_marks_parent_deleted_without_deleting_db(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    deleted = []
    monkeypatch.setattr(svc.RcloneService, "delete_remote_file", lambda *args, **kwargs: deleted.append(args))
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")
    svc.approve_segmentation(session, parent.id, approved_by="admin")

    svc.delete_original_after_approval(session, parent.id)

    assert deleted
    assert session.get(Asset, parent.id) is parent
    assert parent.original_delete_status == "deleted"
    assert parent.original_deleted_at is not None
    assert parent.source_status == "deleted"
    assert parent.auto_select_enabled is False
    assert session.scalar(select(Asset).where(Asset.parent_asset_id == parent.id)) is not None


def test_missing_long_parent_not_eligible_for_selection(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = seed_parent(session)
    patch_successful_pipeline(monkeypatch)
    svc.segment_long_video_asset(session, parent.id, derived_remote="derived", derived_root="")

    candidates = session.scalars(select(Asset).where(eligible_selection_base_filter())).all()

    assert parent not in candidates
