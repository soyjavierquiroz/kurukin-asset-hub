from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import check_database_ready, get_db_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.head("/healthz")
def healthz_head() -> None:
    return None


@router.get("/readyz")
def readyz(session: Session = Depends(get_db_session)) -> dict[str, str]:
    check_database_ready(session)
    return {"status": "ready"}
