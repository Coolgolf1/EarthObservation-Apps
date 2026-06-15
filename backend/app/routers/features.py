import json
import math
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Full, Queue
from threading import Event, Thread, current_thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from collections.abc import Iterator
from typing import TypedDict, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
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
    stop_event: Event,
    chunk_size: int = 250,
) -> Iterator[dict[str, object]]:
    params = _build_source_query_params(bbox_obj=bbox_obj, category=category, limit=limit)

    with get_db_connection() as conn:
        with conn.cursor(name=cursor_name, row_factory=dict_row) as cur:
            cur.execute(cast(PsycopgQuery, query_sql), params)
            while not stop_event.is_set():
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    if stop_event.is_set():
                        break
                    yield row


def _iter_feature_rows_concurrently(bbox_obj: BBox, category: str | None, limit: int) -> Iterator[dict[str, object]]:
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
                stop_event=stop_event,
            ):
                if stop_event.is_set():
                    break
                while not stop_event.is_set():
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
    limit: int = Query(default=1000, ge=1),
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
    categories: str = Query(default="places,transport,hydrography,administrative"),
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
        requested_categories = ["places", "transport", "hydrography", "administrative"]

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
    limit: int = Query(default=1000, ge=1),
):
    bbox_obj = _parse_bbox_param(bbox)

    def stream() -> Iterator[str]:
        emitted = 0
        for row in _iter_feature_rows_concurrently(bbox_obj=bbox_obj, category=category, limit=limit):
            feature = _row_to_feature(row)
            if feature is None:
                continue
            emitted += 1
            yield feature.model_dump_json() + "\n"

        yield json.dumps({"meta": {"count": emitted, "done": True}}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


