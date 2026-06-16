import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Full, Queue
from threading import Event, Lock, Semaphore, Thread, current_thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from collections.abc import Iterator
from typing import TypedDict, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from ..core.db import get_db_connection
from ..models.bbox import BBox
from ..models.features import Feature, Geometry
from psycopg.abc import Query as PsycopgQuery
from psycopg.rows import dict_row


class GeometryDict(TypedDict):
    type: str
    coordinates: list | float


PropertyValue = str | int | float | bool | None


router = APIRouter(prefix="/features", tags=["features"])


NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
ESRI_GEOCODE_URL = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
)
VEHICLE_SPEED_KMH = {
    "walking": 5.0,
    "foot-walking": 5.0,
    "cycling": 16.0,
    "cycling-regular": 16.0,
    "driving": 45.0,
    "driving-car": 45.0,
    "driving-hgv": 35.0,
}

SEARCH_MODE_DEFAULT_LIMIT = 12000
SEARCH_MODE_MAX_LIMIT = 12000
_GEOCODE_CACHE: dict[tuple[str, str | None], dict[str, object]] = {}
MAX_GLOBAL_STREAM_JOBS = max(2, int(os.getenv("MAX_GLOBAL_STREAM_JOBS", "8")))
MAX_CLIENT_STREAM_JOBS = 1
_GLOBAL_STREAM_SEMAPHORE = Semaphore(MAX_GLOBAL_STREAM_JOBS)
_STREAM_COUNTER_LOCK = Lock()
_ACTIVE_STREAMS_BY_CLIENT: dict[str, int] = {}
_SEARCH_STATE_LOCK = Lock()
_ACTIVE_SEARCH_BY_CLIENT: dict[str, tuple[str, Event]] = {}
LLM_API_URL_DEFAULT = "https://api.openai.com/v1/chat/completions"
OPENROUTER_API_URL_DEFAULT = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL_DEFAULT = os.getenv("LLM_MODEL", "qwen/qwen3-next-80b-a3b-instruct:free")


class AgenticSearchRequest(BaseModel):
    prompt: str = Field(min_length=2)
    client_id: str | None = None
    search_id: str | None = None
    llm_api_key: str | None = Field(default=None)
    llm_model: str = Field(default=LLM_MODEL_DEFAULT)
    vehicle: str = Field(default="driving")
    area_mode: str = Field(default="time")
    minutes: int = Field(default=30, ge=1, le=240)
    radius_km: float = Field(default=10.0, ge=0.5, le=500.0)
    metadata_only: bool = Field(default=True)
    categories: str = Field(default="places,transport,hydrography")
    limit: int = Field(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1, le=50000)
    country_hint: str | None = None


class AgenticInterpretation(BaseModel):
    place_query: str
    mode: str
    vehicle: str
    minutes: int
    radius_km: float | None = None
    parser: str


class PointSearchRequest(BaseModel):
    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    client_id: str | None = None
    search_id: str | None = None
    vehicle: str = Field(default="driving")
    area_mode: str = Field(default="time")
    minutes: int = Field(default=30, ge=1, le=240)
    radius_km: float = Field(default=10.0, ge=0.5, le=500.0)
    metadata_only: bool = Field(default=True)
    categories: str = Field(default="places,transport,hydrography")
    limit: int = Field(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1, le=50000)


class BBoxSearchRequest(BaseModel):
    polygon: list[list[float]] = Field(min_length=3)
    client_id: str | None = None
    search_id: str | None = None
    metadata_only: bool = Field(default=True)
    categories: str = Field(default="places,transport,hydrography")
    limit: int = Field(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1, le=50000)


def _ring_to_polygon_geojson(ring: list[list[float]]) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [ring],
    }


def _polygon_bounds(points: list[list[float]]) -> BBox:
    min_lon = min(p[0] for p in points)
    max_lon = max(p[0] for p in points)
    min_lat = min(p[1] for p in points)
    max_lat = max(p[1] for p in points)
    return BBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)


def _sanitize_token(value: str | None) -> str:
    token = (value or "").strip()
    return token or uuid4().hex


def _register_active_search(client_id: str, search_id: str) -> Event:
    with _SEARCH_STATE_LOCK:
        previous = _ACTIVE_SEARCH_BY_CLIENT.get(client_id)
        if previous is not None:
            previous_search_id, previous_cancel_event = previous
            if previous_search_id != search_id:
                previous_cancel_event.set()

        cancel_event = Event()
        _ACTIVE_SEARCH_BY_CLIENT[client_id] = (search_id, cancel_event)
        return cancel_event


def _is_current_search(client_id: str, search_id: str) -> bool:
    with _SEARCH_STATE_LOCK:
        active = _ACTIVE_SEARCH_BY_CLIENT.get(client_id)
        if active is None:
            return False
        active_search_id, _ = active
        return active_search_id == search_id


def _get_search_cancel_event(client_id: str, search_id: str) -> Event | None:
    with _SEARCH_STATE_LOCK:
        active = _ACTIVE_SEARCH_BY_CLIENT.get(client_id)
        if active is None:
            return None
        active_search_id, cancel_event = active
        if active_search_id != search_id:
            return None
        return cancel_event


def _try_acquire_stream_slot(client_id: str) -> bool:
    if not _GLOBAL_STREAM_SEMAPHORE.acquire(blocking=False):
        return False

    with _STREAM_COUNTER_LOCK:
        active_count = _ACTIVE_STREAMS_BY_CLIENT.get(client_id, 0)
        if active_count >= MAX_CLIENT_STREAM_JOBS:
            _GLOBAL_STREAM_SEMAPHORE.release()
            return False

        _ACTIVE_STREAMS_BY_CLIENT[client_id] = active_count + 1
        return True


def _release_stream_slot(client_id: str) -> None:
    with _STREAM_COUNTER_LOCK:
        active_count = _ACTIVE_STREAMS_BY_CLIENT.get(client_id, 0)
        if active_count <= 1:
            _ACTIVE_STREAMS_BY_CLIENT.pop(client_id, None)
        else:
            _ACTIVE_STREAMS_BY_CLIENT[client_id] = active_count - 1

    _GLOBAL_STREAM_SEMAPHORE.release()


NATURAL_EARTH_QUERY = """
WITH bounds AS (
    SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS query_envelope
)
SELECT
    n.id,
    n.name,
    n.category,
    n.subcategory,
    ST_AsGeoJSON(ST_GeomFromWKB(n.geom, 4326)) AS geom,
    'natural_earth'::text AS source
FROM public.natural_earth_silver n
CROSS JOIN bounds b
WHERE (%s::text IS NULL OR lower(COALESCE(n.category::text, '')) = lower(%s::text))
    AND n.geom IS NOT NULL
    AND ST_Intersects(ST_GeomFromWKB(n.geom, 4326), b.query_envelope)
LIMIT %s;
"""


GADM_QUERY = """
WITH bounds AS (
    SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS query_envelope
)
SELECT
    g.id,
    g.name,
    g.category,
    g.subcategory,
    ST_AsGeoJSON(ST_GeomFromWKB(g.geom, 4326)) AS geom,
    'gadm'::text AS source
FROM public.gadm_silver g
CROSS JOIN bounds b
WHERE (%s::text IS NULL OR lower(COALESCE(g.category::text, '')) = lower(%s::text))
    AND g.geom IS NOT NULL
    AND ST_Intersects(ST_GeomFromWKB(g.geom, 4326), b.query_envelope)
LIMIT %s;
"""


FEATURES_QUERY = """
WITH bounds AS (
    SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS query_envelope
)
SELECT
    n.id,
    n.name,
    n.category,
    n.subcategory,
    ST_AsGeoJSON(ST_GeomFromWKB(n.geom, 4326)) AS geom,
    'natural_earth'::text AS source
FROM public.natural_earth_silver n
CROSS JOIN bounds b
WHERE (%s::text IS NULL OR lower(COALESCE(n.category::text, '')) = lower(%s::text))
    AND n.geom IS NOT NULL
    AND ST_Intersects(ST_GeomFromWKB(n.geom, 4326), b.query_envelope)

UNION ALL

SELECT
    g.id,
    g.name,
    g.category,
    g.subcategory,
    ST_AsGeoJSON(ST_GeomFromWKB(g.geom, 4326)) AS geom,
    'gadm'::text AS source
FROM public.gadm_silver g
CROSS JOIN bounds b
WHERE (%s::text IS NULL OR lower(COALESCE(g.category::text, '')) = lower(%s::text))
    AND g.geom IS NOT NULL
    AND ST_Intersects(ST_GeomFromWKB(g.geom, 4326), b.query_envelope)
LIMIT %s;
"""


def _parse_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _extract_geometry(coords_value: object) -> GeometryDict | None:
    if isinstance(coords_value, dict):
        if coords_value.get("type") == "Feature":
            geometry = coords_value.get("geometry")
            if isinstance(geometry, dict) and "type" in geometry and "coordinates" in geometry:
                return cast(GeometryDict, geometry)

        if "type" in coords_value and "coordinates" in coords_value:
            return cast(GeometryDict, coords_value)
    return None


def _parse_bbox_param(value: str) -> BBox:
    raw = value.strip().strip('"').strip("'")
    parts = [part.strip().strip('"').strip("'") for part in raw.split(",")]
    if len(parts) != 4:
        raise HTTPException(
            status_code=422,
            detail="bbox must have 4 comma-separated values: min_lon,min_lat,max_lon,max_lat",
        )

    try:
        min_lon, min_lat, max_lon, max_lat = (float(part) for part in parts)
        return BBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid bbox value: {exc}") from exc


def _build_query_params(bbox_obj: BBox, category: str | None, limit: int) -> tuple[object, ...]:
    return (
        bbox_obj.min_lon,
        bbox_obj.min_lat,
        bbox_obj.max_lon,
        bbox_obj.max_lat,
        category,
        category,
        category,
        category,
        limit,
    )


def _build_source_query_params(bbox_obj: BBox, category: str | None, limit: int) -> tuple[object, ...]:
    return (
        bbox_obj.min_lon,
        bbox_obj.min_lat,
        bbox_obj.max_lon,
        bbox_obj.max_lat,
        category,
        category,
        limit,
    )


def _normalize_vehicle(vehicle: str) -> str:
    key = vehicle.strip().lower()
    if key in VEHICLE_SPEED_KMH:
        return key
    raise HTTPException(
        status_code=422,
        detail=(
            "Unsupported vehicle/profile. Use one of: "
            + ", ".join(sorted(VEHICLE_SPEED_KMH.keys()))
        ),
    )


def _normalize_area_mode(mode: str | None, fallback: str = "time") -> str:
    raw = (mode or fallback).strip().lower()
    if raw in {"time", "minutes", "min"}:
        return "time"
    if raw in {"radius", "km", "distance"}:
        return "radius"
    return fallback


def _normalize_vehicle_flexible(vehicle: str | None, fallback: str = "driving") -> str:
    aliases = {
        "car": "driving",
        "drive": "driving",
        "driving-car": "driving",
        "auto": "driving",
        "truck": "driving-hgv",
        "hgv": "driving-hgv",
        "foot": "walking",
        "walk": "walking",
        "bike": "cycling",
        "bicycle": "cycling",
    }

    candidate = (vehicle or fallback).strip().lower()
    candidate = aliases.get(candidate, candidate)
    try:
        return _normalize_vehicle(candidate)
    except HTTPException:
        return _normalize_vehicle(fallback)


def _geocode_place(place_query: str, country_hint: str | None = None) -> dict[str, object] | None:
    cache_key = (place_query.strip().lower(), country_hint.strip().lower() if country_hint else None)
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Prefer ESRI geocoding so search-mode center coordinates match ESRI semantics.
    esri_params: dict[str, str] = {
        "f": "json",
        "singleLine": place_query,
        "maxLocations": "1",
        "outFields": "*",
    }
    if country_hint:
        esri_params["countryCode"] = country_hint

    esri_url = f"{ESRI_GEOCODE_URL}?{urlencode(esri_params)}"
    esri_req = Request(esri_url, headers={"User-Agent": "EarthMapper/1.0 (search-mode-endpoint)"})
    try:
        with urlopen(esri_req, timeout=8) as resp:
            esri_payload = json.loads(resp.read().decode("utf-8"))

        candidates = esri_payload.get("candidates", []) if isinstance(esri_payload, dict) else []
        if candidates and isinstance(candidates[0], dict):
            loc = candidates[0].get("location") or {}
            if isinstance(loc, dict) and "x" in loc and "y" in loc:
                result = {
                    "name": candidates[0].get("address") or place_query,
                    "lon": float(loc["x"]),
                    "lat": float(loc["y"]),
                    "source": "esri",
                }
                _GEOCODE_CACHE[cache_key] = result
                return result
    except Exception:
        pass

    params: dict[str, str] = {
        "q": place_query,
        "format": "jsonv2",
        "limit": "1",
    }
    if country_hint:
        params["countrycodes"] = country_hint.lower()

    url = f"{NOMINATIM_SEARCH_URL}?{urlencode(params)}"
    req = Request(
        url,
        headers={"User-Agent": "EarthMapper/1.0 (search-mode-endpoint)"},
    )

    try:
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Geocoding provider failed: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        return None

    row = payload[0]
    if not isinstance(row, dict):
        return None

    try:
        result = {
            "name": row.get("display_name") or place_query,
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "source": "nominatim",
        }
        _GEOCODE_CACHE[cache_key] = result
        return result
    except Exception:
        return None


def _bbox_from_reach(center_lon: float, center_lat: float, minutes: int, vehicle: str) -> BBox:
    speed_kmh = VEHICLE_SPEED_KMH[vehicle]
    radius_km = max(0.5, speed_kmh * (minutes / 60.0))

    lat_delta = max(0.01, radius_km / 110.574)
    lon_denominator = 111.320 * max(0.15, math.cos(math.radians(center_lat)))
    lon_delta = max(0.01, radius_km / lon_denominator)

    return BBox(
        min_lon=center_lon - lon_delta,
        min_lat=center_lat - lat_delta,
        max_lon=center_lon + lon_delta,
        max_lat=center_lat + lat_delta,
    )


def _bbox_from_radius(center_lon: float, center_lat: float, radius_km: float) -> BBox:
    safe_radius = max(0.5, float(radius_km))
    lat_delta = max(0.01, safe_radius / 110.574)
    lon_denominator = 111.320 * max(0.15, math.cos(math.radians(center_lat)))
    lon_delta = max(0.01, safe_radius / lon_denominator)

    return BBox(
        min_lon=center_lon - lon_delta,
        min_lat=center_lat - lat_delta,
        max_lon=center_lon + lon_delta,
        max_lat=center_lat + lat_delta,
    )


def _reach_area_geojson(center_lon: float, center_lat: float, minutes: int, vehicle: str, points: int = 72) -> dict[str, object]:
    speed_kmh = VEHICLE_SPEED_KMH[vehicle]
    radius_km = max(0.5, speed_kmh * (minutes / 60.0))

    lat_delta = max(0.01, radius_km / 110.574)
    lon_denominator = 111.320 * max(0.15, math.cos(math.radians(center_lat)))
    lon_delta = max(0.01, radius_km / lon_denominator)

    ring: list[list[float]] = []
    for i in range(points):
        angle = (2.0 * math.pi * i) / points
        lon = center_lon + (lon_delta * math.cos(angle))
        lat = center_lat + (lat_delta * math.sin(angle))
        ring.append([lon, lat])
    ring.append(ring[0])

    return {
        "type": "Polygon",
        "coordinates": [ring],
    }


def _radius_area_geojson(center_lon: float, center_lat: float, radius_km: float, points: int = 72) -> dict[str, object]:
    safe_radius = max(0.5, float(radius_km))
    lat_delta = max(0.01, safe_radius / 110.574)
    lon_denominator = 111.320 * max(0.15, math.cos(math.radians(center_lat)))
    lon_delta = max(0.01, safe_radius / lon_denominator)

    ring: list[list[float]] = []
    for i in range(points):
        angle = (2.0 * math.pi * i) / points
        lon = center_lon + (lon_delta * math.cos(angle))
        lat = center_lat + (lat_delta * math.sin(angle))
        ring.append([lon, lat])
    ring.append(ring[0])

    return {
        "type": "Polygon",
        "coordinates": [ring],
    }


def _extract_first_json_object(raw_text: str) -> dict[str, object] | None:
    text = raw_text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None

    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_agent_prompt_with_llm(
    prompt: str,
    llm_api_key: str | None,
    llm_model: str,
    selected_vehicle: str,
    selected_minutes: int,
) -> dict[str, object]:
    api_key = (llm_api_key or os.getenv("LLM_API_KEY", "")).strip()
    if not api_key:
        raise ValueError("Missing LLM API key. Set LLM_API_KEY in backend .env or provide llm_api_key.")

    env_api_url = (os.getenv("LLM_API_URL", "") or "").strip()
    if env_api_url:
        api_url = env_api_url
    elif api_key.startswith("sk-or-"):
        api_url = OPENROUTER_API_URL_DEFAULT
    else:
        api_url = LLM_API_URL_DEFAULT

    referer = (os.getenv("OPENROUTER_HTTP_REFERER", "") or "").strip()
    app_title = (os.getenv("OPENROUTER_APP_TITLE", "EarthSearch") or "EarthSearch").strip()
    system_prompt = (
        "You extract geospatial search intent from user text. "
        "Return ONLY valid JSON with keys: "
        "place_query (string), mode (one of: default,time,radius), "
        "minutes (int|null), radius_km (number|null), vehicle (string|null), country_hint (string|null). "
        "selected_vehicle and selected_minutes are authoritative UI choices for result computation. "
        "Rules: If the user specifies radius in km, mode=radius and radius_km set. "
        "If the user specifies travel time, mode=time and minutes can be set. "
        "If not specified, mode=default with null minutes/radius_km. "
        "Do not add prose."
    )

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"selected_vehicle={selected_vehicle}; selected_minutes={selected_minutes}; "
                    f"input={prompt}"
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in api_url:
        headers["X-Title"] = app_title
        if referer:
            headers["HTTP-Referer"] = referer

    req = Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    choices = body.get("choices", []) if isinstance(body, dict) else []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("LLM response did not include choices")

    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str):
        raise ValueError("LLM response content is missing")

    parsed = _extract_first_json_object(content)
    if parsed is None:
        raise ValueError("LLM response is not valid JSON")

    return parsed


def _parse_agent_prompt_fallback(
    prompt: str,
    default_vehicle: str,
    default_minutes: int,
) -> dict[str, object]:
    text = prompt.strip()

    minutes_match = re.search(r"(\d{1,3})\s*(min|mins|minute|minutes)\b", text, flags=re.IGNORECASE)
    radius_match = re.search(r"(\d{1,4}(?:\.\d+)?)\s*(km|kilometer|kilometers)\b", text, flags=re.IGNORECASE)

    lowered = text.lower()
    vehicle = default_vehicle
    if any(t in lowered for t in ["walking", "walk", "on foot"]):
        vehicle = "walking"
    elif any(t in lowered for t in ["bike", "bicycle", "cycling", "cycle"]):
        vehicle = "cycling"
    elif any(t in lowered for t in ["truck", "hgv"]):
        vehicle = "driving-hgv"
    elif any(t in lowered for t in ["car", "drive", "driving"]):
        vehicle = "driving"

    place = re.split(
        r"\b(within|in|for|radius|km|min|mins|minute|minutes|by|using)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,")
    if not place:
        place = text

    mode = "default"
    minutes: int | None = None
    radius_km: float | None = None

    if radius_match:
        mode = "radius"
        radius_km = float(radius_match.group(1))
    elif minutes_match:
        mode = "time"
        minutes = int(minutes_match.group(1))

    return {
        "place_query": place,
        "mode": mode,
        "minutes": minutes,
        "radius_km": radius_km,
        "vehicle": vehicle,
        "country_hint": None,
    }


def _extract_prompt_minutes(prompt: str) -> int | None:
    text = prompt.lower()
    hour_match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)\b", text)
    if hour_match:
        try:
            return max(1, int(float(hour_match.group(1)) * 60.0))
        except Exception:
            return None

    min_match = re.search(r"(\d{1,3})\s*(min|mins|minute|minutes)\b", text)
    if min_match:
        try:
            return max(1, int(min_match.group(1)))
        except Exception:
            return None

    return None


def _extract_prompt_radius_km(prompt: str) -> float | None:
    text = prompt.lower()
    match = re.search(r"(\d{1,4}(?:\.\d+)?)\s*(km|kilometer|kilometers|kilometre|kilometres)\b", text)
    if not match:
        return None
    try:
        return max(0.5, float(match.group(1)))
    except Exception:
        return None


def _extract_prompt_vehicle(prompt: str) -> str | None:
    text = prompt.lower()
    if any(t in text for t in ["walking", "walk", "on foot"]):
        return "walking"
    if any(t in text for t in ["bike", "bicycle", "cycling", "cycle"]):
        return "cycling"
    if any(t in text for t in ["truck", "hgv", "lorry"]):
        return "driving-hgv"
    if any(t in text for t in ["car", "drive", "driving"]):
        return "driving"
    return None


def _resolve_agentic_plan(req: AgenticSearchRequest) -> tuple[dict[str, object], str]:
    try:
        parsed = _parse_agent_prompt_with_llm(
            prompt=req.prompt,
            llm_api_key=req.llm_api_key,
            llm_model=req.llm_model,
            selected_vehicle=req.vehicle,
            selected_minutes=req.minutes,
        )
        return parsed, "llm"
    except Exception:
        fallback = _parse_agent_prompt_fallback(
            prompt=req.prompt,
            default_vehicle=req.vehicle,
            default_minutes=req.minutes,
        )
        return fallback, "fallback"


def _fetch_features_for_categories(
    bbox_obj: BBox,
    categories: list[str],
    total_limit: int,
) -> list[Feature]:
    if total_limit <= 0 or not categories:
        return []

    categories_count = len(categories)
    per_category_limit = max(1, total_limit // categories_count)
    remainder = total_limit - (per_category_limit * categories_count)

    def fetch_for_category(index: int, category: str) -> list[Feature]:
        local_limit = per_category_limit + (1 if index < remainder else 0)
        if local_limit <= 0:
            return []

        params = _build_query_params(bbox_obj=bbox_obj, category=category, limit=local_limit)
        local_features: list[Feature] = []
        with get_db_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(FEATURES_QUERY, params)
                rows = cur.fetchall()

        for row in rows:
            feature = _row_to_feature(row)
            if feature is not None:
                local_features.append(feature)

        return local_features

    result: list[Feature] = []
    max_workers = min(4, categories_count)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(fetch_for_category, i, category)
            for i, category in enumerate(categories)
        ]
        for future in futures:
            result.extend(future.result())

    if len(result) <= total_limit:
        return result
    return result[:total_limit]


def _row_to_feature(row: dict[str, object]) -> Feature | None:
    geometry_value = _extract_geometry(_parse_json(row.get("geom")))
    if geometry_value is None:
        return None

    properties: dict[str, PropertyValue] = {}
    for key, value in row.items():
        if key == "geom" or value is None:
            continue

        if isinstance(value, (str, int, float, bool)):
            properties[key] = value
        else:
            properties[key] = str(value)

    return Feature(geometry=Geometry(**geometry_value), properties=properties)


def _row_has_non_empty_name(row: dict[str, object]) -> bool:
    name_value = row.get("name")
    if not isinstance(name_value, str):
        return False
    return bool(name_value.strip())


def _iter_feature_rows(bbox_obj: BBox, category: str | None, limit: int, chunk_size: int = 250) -> Iterator[dict[str, object]]:
    params = _build_query_params(bbox_obj=bbox_obj, category=category, limit=limit)

    with get_db_connection() as conn:
        with conn.cursor(name="feature_stream_cursor", row_factory=dict_row) as cur:
            cur.execute(FEATURES_QUERY, params)
            while True:
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    yield row


def _iter_source_feature_rows(
    query_sql: str,
    cursor_name: str,
    bbox_obj: BBox,
    category: str | None,
    limit: int,
    places_named_only: bool,
    stop_event: Event,
    cancel_event: Event | None = None,
    chunk_size: int = 250,
) -> Iterator[dict[str, object]]:
    params = _build_source_query_params(bbox_obj=bbox_obj, category=category, limit=limit)

    with get_db_connection() as conn:
        with conn.cursor(name=cursor_name, row_factory=dict_row) as cur:
            cur.execute(cast(PsycopgQuery, query_sql), params)
            while not stop_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    if stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        break
                    row_category = str(row.get("category") or "").strip().lower()
                    if places_named_only and row_category == "places" and not _row_has_non_empty_name(row):
                        continue
                    yield row


def _iter_feature_rows_concurrently(
    bbox_obj: BBox,
    category: str | None,
    limit: int,
    places_named_only: bool = False,
    cancel_event: Event | None = None,
) -> Iterator[dict[str, object]]:
    queue: Queue[dict[str, object] | object] = Queue(maxsize=500)
    done_sentinel = object()
    stop_event = Event()

    def producer(query: str, cursor_name: str) -> None:
        try:
            for row in _iter_source_feature_rows(
                query_sql=query,
                cursor_name=cursor_name,
                bbox_obj=bbox_obj,
                category=category,
                limit=limit,
                places_named_only=places_named_only,
                stop_event=stop_event,
                cancel_event=cancel_event,
            ):
                if stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                    break
                while not stop_event.is_set() and not (cancel_event is not None and cancel_event.is_set()):
                    try:
                        queue.put(row, timeout=0.1)
                        break
                    except Full:
                        continue
        finally:
            while True:
                try:
                    queue.put(done_sentinel, timeout=0.1)
                    break
                except Full:
                    if stop_event.is_set():
                        break

    request_key = uuid4().hex
    workers = [
        Thread(
            target=producer,
            args=(NATURAL_EARTH_QUERY, f"stream_ne_{request_key}"),
            daemon=True,
        ),
        Thread(
            target=producer,
            args=(GADM_QUERY, f"stream_gadm_{request_key}"),
            daemon=True,
        ),
    ]

    for worker in workers:
        worker.start()

    done_workers = 0
    emitted = 0
    try:
        while done_workers < len(workers) and emitted < limit:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                item = queue.get(timeout=0.25)
            except Empty:
                continue

            if item is done_sentinel:
                done_workers += 1
                continue

            if not isinstance(item, dict):
                continue

            emitted += 1
            yield item
    finally:
        stop_event.set()
        for worker in workers:
            if worker is current_thread():
                continue
            if worker.is_alive():
                worker.join(timeout=0.5)


@router.get("", response_model=list[Feature])
async def get_features(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    category: str = Query(default="places"),
    limit: int = Query(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1),
):
    bbox_obj = _parse_bbox_param(bbox)

    params = _build_query_params(bbox_obj=bbox_obj, category=category, limit=limit)

    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(FEATURES_QUERY, params)
            rows = cur.fetchall()

    features: list[Feature] = []
    for row in rows:
        feature = _row_to_feature(row)
        if feature is not None:
            features.append(feature)

    return features


@router.get("/search-mode")
async def search_mode_features(
    place_query: str = Query(..., min_length=2),
    minutes: int = Query(default=30, ge=1, le=240),
    vehicle: str = Query(default="driving"),
    bbox: str | None = Query(default=None, description="Optional explicit bbox: min_lon,min_lat,max_lon,max_lat"),
    categories: str = Query(default="places,transport,hydrography"),
    limit: int = Query(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1, le=50000),
    metadata_only: bool = Query(default=False),
    country_hint: str | None = Query(default=None),
):
    normalized_vehicle = _normalize_vehicle(vehicle)
    geocoded = _geocode_place(place_query=place_query, country_hint=country_hint)
    if geocoded is None:
        raise HTTPException(status_code=404, detail="Place not found")

    if bbox:
        bbox_obj = _parse_bbox_param(bbox)
    else:
        bbox_obj = _bbox_from_reach(
            center_lon=cast(float, geocoded["lon"]),
            center_lat=cast(float, geocoded["lat"]),
            minutes=minutes,
            vehicle=normalized_vehicle,
        )

    requested_categories = [c.strip().lower() for c in categories.split(",") if c.strip()]
    if not requested_categories:
        requested_categories = ["places", "transport", "hydrography"]

    effective_limit = min(limit, SEARCH_MODE_MAX_LIMIT)

    features: list[Feature] = []
    if not metadata_only:
        features = _fetch_features_for_categories(
            bbox_obj=bbox_obj,
            categories=requested_categories,
            total_limit=effective_limit,
        )

    center_lon = cast(float, geocoded["lon"])
    center_lat = cast(float, geocoded["lat"])

    return {
        "place_query": place_query,
        "place_name": geocoded["name"],
        "center": {
            "lon": center_lon,
            "lat": center_lat,
        },
        "center_source": geocoded.get("source", "unknown"),
        "minutes": minutes,
        "vehicle": normalized_vehicle,
        "bbox": list(bbox_obj.as_tuple()),
        "categories": requested_categories,
        "reach_area": _reach_area_geojson(
            center_lon=center_lon,
            center_lat=center_lat,
            minutes=minutes,
            vehicle=normalized_vehicle,
        ),
        "metadata_only": metadata_only,
        "limit": effective_limit,
        "requested_limit": limit,
        "features": features,
        "count": len(features),
    }


@router.get("/stream")
async def stream_features(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(default=None),
    limit: int = Query(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1),
    places_named_only: bool = Query(default=False),
    client_id: str | None = Query(default=None),
    search_id: str | None = Query(default=None),
):
    bbox_obj = _parse_bbox_param(bbox)
    if client_id and search_id:
        norm_client_id = _sanitize_token(client_id)
        norm_search_id = _sanitize_token(search_id)
        if not _is_current_search(norm_client_id, norm_search_id):
            raise HTTPException(status_code=409, detail="Stale search id")
    else:
        # Backward-compatible session for simple viewport stream clients (e.g. EarthMapper).
        norm_client_id = _sanitize_token(None)
        norm_search_id = _sanitize_token(None)
        _register_active_search(norm_client_id, norm_search_id)

    if not _try_acquire_stream_slot(norm_client_id):
        raise HTTPException(status_code=429, detail="Too many active streams")

    cancel_event = _get_search_cancel_event(norm_client_id, norm_search_id)
    if cancel_event is None:
        _release_stream_slot(norm_client_id)
        raise HTTPException(status_code=409, detail="Stale search id")

    def stream() -> Iterator[str]:
        emitted = 0
        try:
            for row in _iter_feature_rows_concurrently(
                bbox_obj=bbox_obj,
                category=category,
                limit=limit,
                places_named_only=places_named_only,
                cancel_event=cancel_event,
            ):
                if not _is_current_search(norm_client_id, norm_search_id):
                    break
                feature = _row_to_feature(row)
                if feature is None:
                    continue
                emitted += 1
                yield feature.model_dump_json() + "\n"

            yield json.dumps({"meta": {"count": emitted, "done": True}}) + "\n"
        finally:
            _release_stream_slot(norm_client_id)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.get("/stream-multi")
async def stream_features_multi(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    categories: str = Query(default="places,transport,hydrography"),
    limit: int = Query(default=SEARCH_MODE_DEFAULT_LIMIT, ge=1),
    places_named_only: bool = Query(default=False),
    client_id: str | None = Query(default=None),
    search_id: str | None = Query(default=None),
):
    bbox_obj = _parse_bbox_param(bbox)
    if client_id and search_id:
        norm_client_id = _sanitize_token(client_id)
        norm_search_id = _sanitize_token(search_id)
        if not _is_current_search(norm_client_id, norm_search_id):
            raise HTTPException(status_code=409, detail="Stale search id")
    else:
        # Backward-compatible session for simple viewport stream clients (e.g. EarthMapper).
        norm_client_id = _sanitize_token(None)
        norm_search_id = _sanitize_token(None)
        _register_active_search(norm_client_id, norm_search_id)

    if not _try_acquire_stream_slot(norm_client_id):
        raise HTTPException(status_code=429, detail="Too many active streams")

    cancel_event = _get_search_cancel_event(norm_client_id, norm_search_id)
    if cancel_event is None:
        _release_stream_slot(norm_client_id)
        raise HTTPException(status_code=409, detail="Stale search id")

    category_list = [c.strip().lower() for c in categories.split(",") if c.strip()]
    if not category_list:
        category_list = ["places", "transport", "hydrography"]

    def stream() -> Iterator[str]:
        emitted = 0
        try:
            per_category_limit = max(1, limit // len(category_list))
            remainder = limit - (per_category_limit * len(category_list))

            for index, category in enumerate(category_list):
                if cancel_event.is_set() or not _is_current_search(norm_client_id, norm_search_id):
                    break

                local_limit = per_category_limit + (1 if index < remainder else 0)
                for row in _iter_feature_rows_concurrently(
                    bbox_obj=bbox_obj,
                    category=category,
                    limit=local_limit,
                    places_named_only=places_named_only,
                    cancel_event=cancel_event,
                ):
                    if cancel_event.is_set() or not _is_current_search(norm_client_id, norm_search_id):
                        break
                    feature = _row_to_feature(row)
                    if feature is None:
                        continue
                    emitted += 1
                    yield feature.model_dump_json() + "\n"

            yield json.dumps({"meta": {"count": emitted, "done": True}}) + "\n"
        finally:
            _release_stream_slot(norm_client_id)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.post("/agent-search")
async def agentic_search(payload: AgenticSearchRequest):
    client_id = _sanitize_token(payload.client_id)
    search_id = _sanitize_token(payload.search_id)
    _register_active_search(client_id, search_id)

    parsed_intent, parser_mode = _resolve_agentic_plan(payload)

    place_query_raw = str(parsed_intent.get("place_query") or payload.prompt).strip()
    if len(place_query_raw) < 2:
        raise HTTPException(status_code=422, detail="Prompt must include a location")

    llm_mode = str(parsed_intent.get("mode") or "default").strip().lower()
    radius_raw = parsed_intent.get("radius_km")

    prompt_minutes = _extract_prompt_minutes(payload.prompt)
    prompt_radius_km = _extract_prompt_radius_km(payload.prompt)
    prompt_vehicle = _extract_prompt_vehicle(payload.prompt)

    normalized_vehicle = _normalize_vehicle_flexible(
        vehicle=prompt_vehicle if prompt_vehicle is not None else payload.vehicle,
        fallback="driving",
    )

    if prompt_minutes is not None:
        minutes = prompt_minutes
    else:
        # If prompt has no explicit time, use UI minutes (with 30-minute fallback).
        minutes = payload.minutes
    minutes = max(1, min(240, int(minutes)))

    if prompt_radius_km is not None:
        radius_km = prompt_radius_km
    else:
        try:
            radius_km = float(radius_raw) if radius_raw is not None else 10.0
        except Exception:
            radius_km = 10.0
    radius_km = max(0.5, min(500.0, radius_km))

    selected_mode = _normalize_area_mode(payload.area_mode, fallback="time")

    # Prompt-explicit radius/time override selected mode.
    if prompt_radius_km is not None:
        mode = "radius"
    elif prompt_minutes is not None:
        mode = "time"
    elif selected_mode == "radius":
        mode = "radius"
        radius_km = max(0.5, min(500.0, float(payload.radius_km)))
        minutes = max(1, min(240, int(payload.minutes)))
    elif selected_mode == "time":
        # Prompt has no explicit time/radius: trust UI time box value.
        mode = "time"
        minutes = max(1, min(240, int(payload.minutes)))
    elif llm_mode == "radius" and radius_raw is not None:
        mode = "radius"
    else:
        mode = "time"

    geocoded = _geocode_place(place_query=place_query_raw, country_hint=payload.country_hint)
    if geocoded is None:
        raise HTTPException(status_code=404, detail="Place not found")

    center_lon = cast(float, geocoded["lon"])
    center_lat = cast(float, geocoded["lat"])

    if mode == "radius":
        bbox_obj = _bbox_from_radius(center_lon=center_lon, center_lat=center_lat, radius_km=radius_km)
        reach_area = _radius_area_geojson(center_lon=center_lon, center_lat=center_lat, radius_km=radius_km)
    else:
        bbox_obj = _bbox_from_reach(
            center_lon=center_lon,
            center_lat=center_lat,
            minutes=minutes,
            vehicle=normalized_vehicle,
        )
        reach_area = _reach_area_geojson(
            center_lon=center_lon,
            center_lat=center_lat,
            minutes=minutes,
            vehicle=normalized_vehicle,
        )

    requested_categories = [c.strip().lower() for c in payload.categories.split(",") if c.strip()]
    if not requested_categories:
        requested_categories = ["places", "transport", "hydrography"]

    effective_limit = min(payload.limit, SEARCH_MODE_MAX_LIMIT)
    features: list[Feature] = []
    if not payload.metadata_only:
        features = _fetch_features_for_categories(
            bbox_obj=bbox_obj,
            categories=requested_categories,
            total_limit=effective_limit,
        )

    return {
        "client_id": client_id,
        "search_id": search_id,
        "prompt": payload.prompt,
        "parser_mode": parser_mode,
        "parsed_intent": {
            "place_query": place_query_raw,
            "mode": mode,
            "minutes": minutes,
            "radius_km": radius_km,
            "vehicle": normalized_vehicle,
            "prompt_overrides": {
                "time": prompt_minutes is not None,
                "radius_km": prompt_radius_km is not None,
                "vehicle": prompt_vehicle is not None,
            },
        },
        "place_name": geocoded["name"],
        "center": {
            "lon": center_lon,
            "lat": center_lat,
        },
        "center_source": geocoded.get("source", "unknown"),
        "minutes": minutes,
        "vehicle": normalized_vehicle,
        "bbox": list(bbox_obj.as_tuple()),
        "categories": requested_categories,
        "reach_area": reach_area,
        "metadata_only": payload.metadata_only,
        "limit": effective_limit,
        "requested_limit": payload.limit,
        "features": features,
        "count": len(features),
    }


@router.post("/point-search")
async def point_search(payload: PointSearchRequest):
    client_id = _sanitize_token(payload.client_id)
    search_id = _sanitize_token(payload.search_id)
    _register_active_search(client_id, search_id)

    normalized_vehicle = _normalize_vehicle_flexible(vehicle=payload.vehicle, fallback="driving")
    mode = _normalize_area_mode(payload.area_mode, fallback="time")
    minutes = max(1, min(240, int(payload.minutes)))
    radius_km = max(0.5, min(500.0, float(payload.radius_km)))

    center_lon = float(payload.lon)
    center_lat = float(payload.lat)

    if mode == "radius":
        bbox_obj = _bbox_from_radius(center_lon=center_lon, center_lat=center_lat, radius_km=radius_km)
        reach_area = _radius_area_geojson(center_lon=center_lon, center_lat=center_lat, radius_km=radius_km)
    else:
        bbox_obj = _bbox_from_reach(
            center_lon=center_lon,
            center_lat=center_lat,
            minutes=minutes,
            vehicle=normalized_vehicle,
        )
        reach_area = _reach_area_geojson(
            center_lon=center_lon,
            center_lat=center_lat,
            minutes=minutes,
            vehicle=normalized_vehicle,
        )

    requested_categories = [c.strip().lower() for c in payload.categories.split(",") if c.strip()]
    if not requested_categories:
        requested_categories = ["places", "transport", "hydrography"]

    effective_limit = min(payload.limit, SEARCH_MODE_MAX_LIMIT)
    features: list[Feature] = []
    if not payload.metadata_only:
        features = _fetch_features_for_categories(
            bbox_obj=bbox_obj,
            categories=requested_categories,
            total_limit=effective_limit,
        )

    return {
        "client_id": client_id,
        "search_id": search_id,
        "prompt": None,
        "parser_mode": "map-click",
        "parsed_intent": {
            "place_query": f"{center_lat:.6f},{center_lon:.6f}",
            "mode": mode,
            "minutes": minutes,
            "radius_km": radius_km,
            "vehicle": normalized_vehicle,
            "prompt_overrides": {
                "time": False,
                "radius_km": False,
                "vehicle": False,
            },
        },
        "place_name": f"Clicked point ({center_lat:.5f}, {center_lon:.5f})",
        "center": {
            "lon": center_lon,
            "lat": center_lat,
        },
        "center_source": "map_click",
        "minutes": minutes,
        "vehicle": normalized_vehicle,
        "bbox": list(bbox_obj.as_tuple()),
        "categories": requested_categories,
        "reach_area": reach_area,
        "metadata_only": payload.metadata_only,
        "limit": effective_limit,
        "requested_limit": payload.limit,
        "features": features,
        "count": len(features),
    }


@router.post("/bbox-search")
async def bbox_search(payload: BBoxSearchRequest):
    client_id = _sanitize_token(payload.client_id)
    search_id = _sanitize_token(payload.search_id)
    _register_active_search(client_id, search_id)

    raw_points = list(payload.polygon)
    if len(raw_points) < 3:
        raise HTTPException(status_code=422, detail="polygon must have at least 3 points")

    polygon_points: list[list[float]] = []
    for idx, point in enumerate(raw_points):
        if not isinstance(point, list) or len(point) < 2:
            raise HTTPException(status_code=422, detail=f"Invalid polygon point at index {idx}")
        lon = float(point[0])
        lat = float(point[1])
        if lon < -180.0 or lon > 180.0 or lat < -90.0 or lat > 90.0:
            raise HTTPException(status_code=422, detail=f"Polygon point out of range at index {idx}")
        polygon_points.append([lon, lat])

    polygon_ring = list(polygon_points)
    if polygon_ring[0] != polygon_ring[-1]:
        polygon_ring.append([polygon_ring[0][0], polygon_ring[0][1]])

    bbox_obj = _polygon_bounds(polygon_points)

    requested_categories = [c.strip().lower() for c in payload.categories.split(",") if c.strip()]
    if not requested_categories:
        requested_categories = ["places", "transport", "hydrography"]

    effective_limit = min(payload.limit, SEARCH_MODE_MAX_LIMIT)
    features: list[Feature] = []
    if not payload.metadata_only:
        features = _fetch_features_for_categories(
            bbox_obj=bbox_obj,
            categories=requested_categories,
            total_limit=effective_limit,
        )

    center_lon = (bbox_obj.min_lon + bbox_obj.max_lon) / 2.0
    center_lat = (bbox_obj.min_lat + bbox_obj.max_lat) / 2.0

    return {
        "client_id": client_id,
        "search_id": search_id,
        "prompt": None,
        "parser_mode": "bbox-selection",
        "parsed_intent": {
            "place_query": f"polygon:{len(polygon_points)}pts",
            "mode": "polygon",
            "minutes": 30,
            "radius_km": None,
            "vehicle": "driving",
            "prompt_overrides": {
                "time": False,
                "radius_km": False,
                "vehicle": False,
            },
        },
        "place_name": "Custom polygon",
        "center": {
            "lon": center_lon,
            "lat": center_lat,
        },
        "center_source": "bbox",
        "minutes": 30,
        "vehicle": "driving",
        "bbox": list(bbox_obj.as_tuple()),
        "categories": requested_categories,
        "reach_area": _ring_to_polygon_geojson(polygon_ring),
        "metadata_only": payload.metadata_only,
        "limit": effective_limit,
        "requested_limit": payload.limit,
        "features": features,
        "count": len(features),
    }


