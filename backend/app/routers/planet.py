import base64
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.log import logger

env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(env_path)

router = APIRouter(prefix="/planet", tags=["planet"])

PLANET_DATA_API_URL = os.getenv("PLANET_DATA_API_URL", "https://api.planet.com/data/v1")


class PlanetQuickSearchRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: str
    date_end: str
    item_type: str = Field(default="PSScene")
    limit: int = Field(default=50, ge=1, le=250)


class PlanetProxyResponse(BaseModel):
    backend_url: str
    status_code: int
    data: dict[str, object]


def _get_planet_api_key() -> str:
    key = (os.getenv("PLANET_API_KEY") or "").strip()
    if not key:
        raise HTTPException(
            status_code=500,
            detail="PLANET_API_KEY is missing. Add it to .env and restart backend.",
        )
    return key


def _to_rfc3339(value: str, *, end_of_day: bool) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("date value cannot be empty")

    if len(raw) == 10:
        return f"{raw}T23:59:59Z" if end_of_day else f"{raw}T00:00:00Z"

    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        return normalized

    if "+" in normalized[10:] or "-" in normalized[10:]:
        return normalized

    return f"{normalized}Z"


def _bbox_to_polygon(bbox: list[float]) -> dict[str, object]:
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _planet_auth_header() -> str:
    key = _get_planet_api_key()
    token = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url=url,
        method="POST",
        data=body,
        headers={
            "Authorization": _planet_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode("utf-8"))
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=502, detail="Planet API returned non-object JSON")
            return parsed
    except HTTPError as exc:
        detail = exc.reason
        try:
            raw = exc.read().decode("utf-8")
            payload = json.loads(raw)
            if isinstance(payload, dict):
                detail = payload.get("message") or payload.get("detail") or raw
            else:
                detail = raw
        except Exception:
            pass
        raise HTTPException(status_code=exc.code or 502, detail=f"Planet API error: {detail}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Planet API request failed")
        raise HTTPException(status_code=502, detail="Planet API request failed") from exc


@router.get("/health")
async def planet_health():
    api_key = _get_planet_api_key()
    return {
        "status": "ok",
        "backend_url": PLANET_DATA_API_URL,
        "api_key_configured": bool(api_key),
    }


@router.post("/quick-search")
async def planet_quick_search(payload: PlanetQuickSearchRequest):
    try:
        start_iso = _to_rfc3339(payload.date_start, end_of_day=False)
        end_iso = _to_rfc3339(payload.date_end, end_of_day=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date range: {exc}") from exc

    if start_iso > end_iso:
        raise HTTPException(status_code=400, detail="date_start must be before or equal to date_end")

    search_payload = {
        "item_types": [payload.item_type],
        "filter": {
            "type": "AndFilter",
            "config": [
                {
                    "type": "GeometryFilter",
                    "field_name": "geometry",
                    "config": _bbox_to_polygon(payload.bbox),
                },
                {
                    "type": "DateRangeFilter",
                    "field_name": "acquired",
                    "config": {
                        "gte": start_iso,
                        "lte": end_iso,
                    },
                },
                {
                    "type": "PermissionFilter",
                    "config": ["assets:download"],
                },
            ],
        },
    }

    api_data = _post_json(f"{PLANET_DATA_API_URL}/quick-search", search_payload)
    features = api_data.get("features")
    if not isinstance(features, list):
        raise HTTPException(status_code=502, detail="Planet API response did not include features list")

    limited = features[: payload.limit]

    return PlanetProxyResponse(
        backend_url=PLANET_DATA_API_URL,
        status_code=200,
        data={
            "type": "FeatureCollection",
            "features": limited,
            "returned": len(limited),
            "item_type": payload.item_type,
            "date_start": start_iso,
            "date_end": end_iso,
        },
    )
