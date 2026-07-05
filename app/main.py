from fastapi import FastAPI

from app.api.assets import router as assets_api_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_api_router
from app.config import get_settings
from app.web.routes import router as web_router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Kurukin Asset Hub",
        version="0.1.0",
        debug=settings.app_env != "production",
    )
    app.include_router(health_router)
    app.include_router(assets_api_router)
    app.include_router(jobs_api_router)
    app.include_router(web_router)
    return app


app = create_app()
