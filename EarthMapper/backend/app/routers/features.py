import json
from queue import Empty, Full, Queue
from threading import Event, Thread, current_thread
from uuid import uuid4
from collections.abc import Iterator
from typing import TypedDict, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from app.core.db import get_db_connection
from app.models.bbox import BBox
from app.models.features import Feature, Geometry
from psycopg.abc import Query as PsycopgQuery
from psycopg.rows import dict_row


class GeometryDict(TypedDict):
    type: str
    coordinates: list | float


PropertyValue = str | int | float | bool | None


router = APIRouter(prefix="/features", tags=["features"])


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
    AND ST_GeomFromWKB(n.geom, 4326) && b.query_envelope
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
    AND ST_GeomFromWKB(g.geom, 4326) && b.query_envelope
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
    AND ST_GeomFromWKB(n.geom, 4326) && b.query_envelope

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
    AND ST_GeomFromWKB(g.geom, 4326) && b.query_envelope
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


@router.get("/stream")
async def stream_features(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    category: str = Query(default="places"),
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


