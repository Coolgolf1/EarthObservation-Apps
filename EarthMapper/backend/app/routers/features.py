import json
from typing import TypedDict, cast

from fastapi import APIRouter, HTTPException, Query
from app.core.db import get_db_connection
from app.models.bbox import BBox
from app.models.features import Feature, Geometry
from psycopg.rows import dict_row


class GeometryDict(TypedDict):
    type: str
    coordinates: list | float


router = APIRouter(prefix="/features", tags=["features"])


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


@router.get("", response_model=list[Feature])
async def get_features(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    category: str = Query(default="places"),
    limit: int = Query(default=1000, ge=1),
):
    bbox_obj = _parse_bbox_param(bbox)

    query = """
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

    params = (
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
    
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    features: list[Feature] = []
    for row in rows:
        geometry_value = _extract_geometry(_parse_json(row.get("geom")))
        if geometry_value is None:
            continue

        properties: dict[str, str | int | float | bool | None] = {
            key: value
            for key, value in row.items()
            if key not in {"geom"} and value is not None
        }

        features.append(Feature(geometry=Geometry(**geometry_value), properties=properties))

    return features


