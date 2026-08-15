from base64 import b64encode
from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db_session
from app.main import create_app
from app.models import Asset, AssetAIAnalysis, Source
from app.schemas.visual_intelligence import VISUAL_ANALYSIS_TYPE, VISUAL_PROFILE_VERSION
from app.web.routes import visual_intelligence_view_model


def auth_header(username: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def make_test_app():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session() -> Generator[Session, None, None]:
        with session_factory() as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    return app, session_factory


def seed_preview_asset(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        source = Source(
            source_id="gdrive_code_x",
            provider="google_drive",
            label="Drive Code X",
            rclone_remote="gdrive_code_x",
            root_path="assets",
        )
        asset = Asset(
            asset_uid="asset_ui_001",
            source=source,
            provider="google_drive",
            rclone_remote="gdrive_code_x",
            remote_path="assets/video.mp4",
            filename="video.mp4",
            type="video",
            preview_status="ready",
            technical_metadata_status="ready",
            thumbnail_path="assets/previews/1/thumbnail.jpg",
            preview_path="assets/previews/1/preview.mp4",
        )
        session.add(asset)
        session.commit()
        return asset.id


def seed_visual_intelligence_asset(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        source = Source(
            source_id="gdrive_vi",
            provider="google_drive",
            label="Drive VI",
            rclone_remote="gdrive_vi",
            root_path="assets",
        )
        asset = Asset(
            asset_uid="asset_vi_001",
            source=source,
            provider="google_drive",
            rclone_remote="gdrive_vi",
            remote_path="assets/visual.mp4",
            filename="visual.mp4",
            type="video",
            editorial_status="quarantined",
            technical_metadata_status="ready",
        )
        session.add(asset)
        session.flush()
        session.add(
            AssetAIAnalysis(
                asset_id=asset.id,
                model="visual-model",
                provider="nvidia",
                input_type=VISUAL_ANALYSIS_TYPE,
                prompt_version=VISUAL_PROFILE_VERSION,
                result_json={
                    "status": "ready",
                    "profile_version": VISUAL_PROFILE_VERSION,
                    "model": "visual-model",
                    "visual": {
                        "quality": {
                            "score": 0.72,
                            "label": "usable",
                            "sharpness": 0.81,
                            "exposure": 0.62,
                            "stability": 0.93,
                            "lighting": 0.58,
                            "temporal_consistency": 0.77,
                            "reason_codes": ["soft_light"],
                        },
                        "garbage": {
                            "score": 0.18,
                            "is_garbage": False,
                            "black_or_blank": False,
                            "subject_severely_out_of_frame": False,
                            "subject_badly_clipped": True,
                            "social_media_ui": False,
                            "subscribe_cta": False,
                            "emoji_overlay": False,
                            "watermark": True,
                            "logo": False,
                            "heavy_text_overlay": False,
                            "nearly_empty": False,
                            "severe_blur": False,
                            "severe_black_frames": False,
                            "corrupted_frames": False,
                            "accidental_capture": False,
                            "editorial_usable": True,
                            "reasons": ["minor_watermark"],
                        },
                        "semantics": {
                            "summary_es": "Persona caminando",
                            "subjects": ["persona"],
                            "actions": ["caminar"],
                            "objects": [],
                            "emotions": [],
                            "narrative_themes": [],
                            "possible_use_cases": [],
                            "negative_use_cases": [],
                            "setting": "street",
                            "mood": "calm",
                            "keywords_es": ["persona"],
                            "contains_people": True,
                            "visible_text": "Cafe",
                            "logo_or_watermark": True,
                        },
                        "composition": {
                            "shot_type": "medium",
                            "subject_position": "center",
                            "subject_framing": "partial",
                            "subject_region": [0.25, 0.20, 0.50, 0.70],
                            "subject_trajectory": [
                                {"timestamp": 0.0, "center": [0.40, 0.50], "bbox": [0.2, 0.2, 0.4, 0.6]},
                                {"timestamp": 1.0, "center": [0.55, 0.52], "bbox": [0.3, 0.2, 0.4, 0.6]},
                            ],
                            "negative_space": 0.22,
                            "edge_proximity": 0.41,
                            "vertical_suitability": 0.66,
                            "horizontal_suitability": 0.74,
                            "camera_motion": "static",
                            "camera_motion_confidence": 0.88,
                            "safe_text_areas": ["top", "bottom"],
                            "crop_risk_reasons": ["visible_text"],
                        },
                    },
                    "normalized_transforms": {
                        "flip_horizontal": {
                            "status": "unknown",
                            "allowed": False,
                            "confidence": 0.48,
                            "blockers": [],
                            "reasons": ["low_confidence"],
                        },
                        "zoom": {
                            "status": "safe",
                            "allowed": True,
                            "confidence": 0.91,
                            "blockers": [],
                            "reasons": [],
                            "max_safe_zoom": 1.08,
                        },
                        "pan": {
                            "status": "safe",
                            "allowed": True,
                            "confidence": 0.82,
                            "blockers": [],
                            "reasons": [],
                            "safe_directions": ["left", "right"],
                            "max_offset_x": 0.06,
                            "max_offset_y": 0.04,
                        },
                        "crop_vertical": {
                            "status": "safe",
                            "allowed": True,
                            "confidence": 0.86,
                            "blockers": [],
                            "reasons": [],
                            "safe_rect": [0.10, 0.00, 0.80, 1.00],
                        },
                        "crop_horizontal": {
                            "status": "unsafe",
                            "allowed": False,
                            "confidence": 0.78,
                            "blockers": ["cuts_text"],
                            "reasons": ["cuts_text"],
                            "safe_rect": [0.00, 0.10, 1.00, 0.80],
                        },
                    },
                    "normalization_warnings": [],
                },
                confidence=0.72,
            )
        )
        session.commit()
        return asset.id


def test_asset_detail_shows_generate_preview_button_and_video() -> None:
    app, session_factory = make_test_app()
    asset_id = seed_preview_asset(session_factory)
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Regenerate Preview" in response.text
    assert "/media/assets/previews/1/preview.mp4" in response.text
    assert "<video" in response.text


def test_visual_intelligence_view_model_summarizes_trajectory_and_transform_status() -> None:
    result = {
        "visual": {
            "composition": {
                "subject_trajectory": [
                    {"center": [0.4, 0.5]},
                    {"center": [0.55, 0.52]},
                ]
            }
        },
        "normalized_transforms": {
            "flip_horizontal": {
                "status": "unknown",
                "confidence": 0.48,
                "blockers": [],
                "reasons": ["low_confidence"],
            }
        },
    }

    view = visual_intelligence_view_model(result)

    assert view["subject_trajectory_summary"] == "2 samples · (0.40, 0.50) -> (0.55, 0.52)"
    assert view["transforms"]["flip_horizontal"]["status"] == "UNKNOWN"
    assert view["transforms"]["flip_horizontal"]["confidence"] == 0.48
    assert view["transforms"]["flip_horizontal"]["blockers"] == []
    assert view["transforms"]["flip_horizontal"]["reasons"] == ["low_confidence"]


def test_asset_detail_renders_visual_intelligence_read_model() -> None:
    app, session_factory = make_test_app()
    asset_id = seed_visual_intelligence_asset(session_factory)
    client = TestClient(app)

    response = client.get(f"/assets/{asset_id}", headers=auth_header())

    assert response.status_code == 200
    assert "Editorial status" in response.text
    assert "quarantined" in response.text
    assert "Analysis status" in response.text
    assert "ready" in response.text
    assert "UNKNOWN\n      · confidence 0.48" in response.text
    assert "Reason: low_confidence" in response.text
    assert "Blockers: none" in response.text
    assert "max_safe_zoom: 1.08" in response.text
    assert "safe_directions: left, right" in response.text
    assert "max_offset_x: 0.06" in response.text
    assert "safe_rect: 0.1, 0.0, 0.8, 1.0" in response.text
    assert "Safe text areas" in response.text
    assert "top, bottom" in response.text
    assert "Crop risk reasons" in response.text
    assert "2 samples · (0.40, 0.50) -&gt; (0.55, 0.52)" in response.text
    assert "timestamp" not in response.text
    assert "Sharpness" in response.text
    assert "Quality reason codes" in response.text
    assert "Garbage score" in response.text
    assert "Editorial usable" in response.text
    assert "Contains people" in response.text
    assert "Visible text" in response.text
    assert "Logo or watermark" in response.text
    assert "Legacy AI Enrichment" in response.text


def test_media_preview_requires_basic_auth() -> None:
    app, _ = make_test_app()
    client = TestClient(app)

    response = client.get("/media/previews/asset_ui_001/thumbnail.jpg")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
