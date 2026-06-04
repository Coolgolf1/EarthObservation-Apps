import json
from typing import Any

from fastapi import APIRouter, Query
from app.core.db import get_db_connection
from app.models.features import Feature, Geometry
from psycopg.rows import dict_row

try:
    from shapely.geometry import shape
    from shapely.wkt import dumps as to_wkt
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

router = APIRouter(prefix="/features", tags=["features"])


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    parsed = value
    for _ in range(2):
        if not isinstance(parsed, str):
            break
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            break
    return parsed


def _infer_geometry_type(coords: Any) -> str | None:
    if not isinstance(coords, list):
        return None

    def _is_position(value: Any) -> bool:
        return (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, (int, float)) for v in value)
        )

    def _is_line(value: Any) -> bool:
        return isinstance(value, list) and len(value) >= 2 and all(_is_position(p) for p in value)

    def _is_linear_ring(value: Any) -> bool:
        if not _is_line(value) or len(value) < 4:
            return False
        return value[0] == value[-1]

    if _is_position(coords):
        return "Point"

    if _is_line(coords):
        return "LineString"

    if isinstance(coords, list) and coords and all(_is_line(part) for part in coords):
        # Distinguish polygon rings from multiline paths to avoid rivers becoming filled lakes.
        if all(_is_linear_ring(ring) for ring in coords):
            return "Polygon"
        return "MultiLineString"

    if isinstance(coords, list) and coords and all(
        isinstance(polygon, list) and polygon and all(_is_linear_ring(ring) for ring in polygon)
        for polygon in coords
    ):
        return "MultiPolygon"

    return None


def _build_geometry_from_coords(coords_value: Any) -> dict[str, Any] | None:
    # Geometry rendering must be based on coords only.
    if isinstance(coords_value, dict):
        if coords_value.get("type") == "Feature":
            maybe_geom = coords_value.get("geometry")
            if isinstance(maybe_geom, dict) and "type" in maybe_geom and "coordinates" in maybe_geom:
                return maybe_geom

        if "type" in coords_value and "coordinates" in coords_value:
            return coords_value

    geometry_type = _infer_geometry_type(coords_value)
    if geometry_type is None:
        return None

    return {
        "type": geometry_type,
        "coordinates": coords_value,
    }


def _simplify_coordinates(coords: Any, tolerance: float) -> Any:
    """Simplify coordinates using Douglas-Peucker algorithm if tolerance > 0."""
    if tolerance <= 0 or not SHAPELY_AVAILABLE:
        return coords
    
    try:
        geometry_type = _infer_geometry_type(coords)
        if not geometry_type:
            return coords
        
        # Skip simplification for Point geometries - they can't be simplified
        if geometry_type == "Point":
            return coords
        
        # Build a full GeoJSON geometry
        geom_dict = {
            "type": geometry_type,
            "coordinates": coords,
        }
        
        # Use Shapely to simplify
        geom = shape(geom_dict)
        simplified = geom.simplify(tolerance, preserve_topology=True)
        
        # Check if simplified geometry is empty
        if simplified.is_empty:
            return coords
        
        # Extract simplified coordinates
        if hasattr(simplified, '__geo_interface__'):
            simplified_dict = simplified.__geo_interface__
            simplified_coords = simplified_dict.get("coordinates")
            
            # Return simplified if we got valid coordinates
            if simplified_coords is not None and len(str(simplified_coords)) > 0:
                return simplified_coords
        
        return coords
    except Exception:
        # If simplification fails, return original
        return coords


@router.get("", response_model=list[Feature])
async def get_features(
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
    category: str = Query(default="places"),
    use_postgis: bool = Query(default=False),
    zoom: int = Query(default=2, ge=0, le=19),
    limit: int = Query(default=1000, ge=1),
):
    query = """
    WITH bounds AS (
        SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS query_envelope
    )
    SELECT
        n.id,
        n.name,
        n.category,
        n.subcategory,
        n.min_lon,
        n.max_lon,
        n.min_lat,
        n.max_lat,
        n.coords,
        'natural_earth'::text AS source
    FROM public.natural_earth_silver n
    WHERE (%s::text IS NULL OR lower(COALESCE(n.category::text, '')) = lower(%s::text))
      AND n.min_lon IS NOT NULL
      AND n.max_lon IS NOT NULL
      AND n.min_lat IS NOT NULL
      AND n.max_lat IS NOT NULL
      AND n.max_lon >= %s
      AND n.min_lon <= %s
      AND n.max_lat >= %s
      AND n.min_lat <= %s
      AND (
          NOT %s::boolean
          OR EXISTS (
              SELECT 1 FROM bounds b
              WHERE ST_MakeEnvelope(n.min_lon, n.min_lat, n.max_lon, n.max_lat, 4326) && b.query_envelope
          )
      )

    UNION ALL

    SELECT
        g.id,
        g.name,
        g.category,
        g.subcategory,
        g.min_lon,
        g.max_lon,
        g.min_lat,
        g.max_lat,
        g.coords,
        'gadm'::text AS source
    FROM public.gadm_silver g
    WHERE (%s::text IS NULL OR lower(COALESCE(g.category::text, '')) = lower(%s::text))
      AND g.min_lon IS NOT NULL
      AND g.max_lon IS NOT NULL
      AND g.min_lat IS NOT NULL
      AND g.max_lat IS NOT NULL
      AND g.max_lon >= %s
      AND g.min_lon <= %s
      AND g.max_lat >= %s
      AND g.min_lat <= %s
      AND (
          NOT %s::boolean
          OR EXISTS (
              SELECT 1 FROM bounds b
              WHERE ST_MakeEnvelope(g.min_lon, g.min_lat, g.max_lon, g.max_lat, 4326) && b.query_envelope
          )
      )
    LIMIT %s;
    """

    # Calculate tolerance based on zoom level
    # Lower tolerance = less simplification = more detail preserved
    if zoom <= 3:
        tolerance = 0.01
    elif zoom <= 6:
        tolerance = 0.001
    elif zoom <= 9:
        tolerance = 0.0001
    else:
        tolerance = 0

    params = (
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        category,
        category,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
        use_postgis,
        category,
        category,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
        use_postgis,
        limit,
    )
    
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    features: list[Feature] = []
    for row in rows:
        coords_value = _parse_json_value(row.get("coords"))
        
        # Skip simplification for now - focus on getting all geometries to render
        # TODO: Re-enable with better type checking to prevent geometry collapse
        
        geom = _build_geometry_from_coords(coords_value)
        if geom is None:
            continue

        props: dict[str, Any] = {}
        if isinstance(coords_value, dict) and coords_value.get("type") == "Feature":
            props = coords_value.get("properties") or {}

        row_properties = {
            key: value
            for key, value in row.items()
            if key not in {"coords", "min_lon", "max_lon", "min_lat", "max_lat"} and value is not None
        }
        props = {**row_properties, **props}

        if not isinstance(geom, dict) or "type" not in geom or "coordinates" not in geom:
            continue

        features.append(Feature(geometry=Geometry(**geom), properties=props))

    return features


