import json

from fastapi import APIRouter, Query
from app.core.db import get_db_connection
from app.models.features import Feature, Geometry
from psycopg.rows import dict_row

router = APIRouter(prefix="/features", tags=["features"])

@router.get("", response_model=list[Feature])
async def get_features(
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
    category: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
):
    # bbox_output = BBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)

    query = """
    SELECT
        n.id,
        n.name,
        n.category,
        n.subcategory,
        n.source,
        n.geometry
    FROM public.natural_earth n
    WHERE (%s::text IS NULL OR lower(COALESCE(n.category::text, '')) = lower(%s::text))
      AND (
          (
              n.longitude IS NOT NULL
              AND n.latitude IS NOT NULL
              AND n.longitude BETWEEN %s AND %s
              AND n.latitude BETWEEN %s AND %s
          )
          OR (
              n.min_bbxmin IS NOT NULL
              AND n.max_bbxmax IS NOT NULL
              AND n.min_bbymin IS NOT NULL
              AND n.max_bbymax IS NOT NULL
              AND n.max_bbxmax >= %s
              AND n.min_bbxmin <= %s
              AND n.max_bbymax >= %s
              AND n.min_bbymin <= %s
          )
          OR (
              n.longitude IS NULL
              AND n.latitude IS NULL
              AND (
                  n.min_bbxmin IS NULL
                  OR n.max_bbxmax IS NULL
                  OR n.min_bbymin IS NULL
                  OR n.max_bbymax IS NULL
              )
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_path_query(
                      CASE
                          WHEN jsonb_typeof(n.geometry) = 'string' THEN
                              CASE
                                  WHEN ((n.geometry #>> '{}')::jsonb->>'type') = 'Feature'
                                      THEN (n.geometry #>> '{}')::jsonb->'geometry'
                                  ELSE (n.geometry #>> '{}')::jsonb
                              END
                          ELSE
                              CASE
                                  WHEN n.geometry->>'type' = 'Feature' THEN n.geometry->'geometry'
                                  ELSE n.geometry
                              END
                      END,
                      '$.coordinates.** ? (@.type() == "array" && @.size() == 2 && @[0].type() == "number" && @[1].type() == "number")'
                  ) AS pt
                  WHERE (pt->>0)::double precision BETWEEN %s AND %s
                    AND (pt->>1)::double precision BETWEEN %s AND %s
              )
          )
      )
        LIMIT %s;
    """

    params = (
        category,
        category,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
        limit,
    )
    
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    features: list[Feature] = []
    for row in rows:
        geometry_value = row.get("geometry")

        if isinstance(geometry_value, str):
            geometry_value = json.loads(geometry_value)
            if isinstance(geometry_value, str):
                geometry_value = json.loads(geometry_value)

        if not isinstance(geometry_value, dict):
            continue

        if geometry_value.get("type") == "Feature":
            geom = geometry_value.get("geometry")
            props = geometry_value.get("properties") or {}
        else:
            geom = geometry_value
            props = {}

        row_properties = {
            key: value
            for key, value in row.items()
            if key != "geometry" and value is not None
        }
        props = {**row_properties, **props}

        if not isinstance(geom, dict) or "type" not in geom or "coordinates" not in geom:
            continue

        features.append(Feature(geometry=Geometry(**geom), properties=props))

    return features


