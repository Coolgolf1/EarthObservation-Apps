from fastapi import APIRouter
from ..core.utils import check_db_connection

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/")
@router.get("")
def health() -> dict[str, str]:
	return {"status": "ok"}


@router.get("/db")
def health_db() -> dict[str, bool]:
	return {"db_ok": check_db_connection()}

