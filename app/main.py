from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Kurukin Asset Hub", version="0.1.0", debug=settings.app_env != "production")
    app.include_router(health_router)
    return app


app = create_app()
