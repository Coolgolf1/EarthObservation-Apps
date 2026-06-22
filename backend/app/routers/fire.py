from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import planetary_computer
import pystac_client
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter(prefix="/fire", tags=["fire"])

CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
MODIS_FIRE_COLLECTION = "modis-14A2-061"


class ModisFireQueryRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: date
    date_end: date
    max_items: int = Field(default=24, ge=1, le=120)
    step_days: int = Field(default=8, ge=1, le=31)


def _validate_bbox(bbox: list[float]) -> tuple[float, float, float, float]:
    west, south, east, north = [float(v) for v in bbox]
    if west >= east or south >= north:
        raise HTTPException(status_code=400, detail="Invalid bbox coordinates")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise HTTPException(status_code=400, detail="bbox out of WGS84 range")
    return west, south, east, north


def _build_datetime_steps(start: date, end: date, step_days: int, max_items: int) -> list[date]:
    steps: list[date] = []
    current = start
    while current <= end and len(steps) < max_items:
        steps.append(current)
        current = current + timedelta(days=step_days)
    return steps


def _query_single_item_for_datetime(catalog: pystac_client.Client, lon: float, lat: float, dt: date) -> Any | None:
    geometry = {
        "type": "Point",
        "coordinates": [lon, lat],
    }

    search = catalog.search(
        collections=[MODIS_FIRE_COLLECTION],
        intersects=geometry,
        datetime=dt.isoformat(),
        limit=1,
    )

    if hasattr(search, "item_collection"):
        found = list(search.item_collection())
    elif hasattr(search, "get_all_items"):
        found = list(search.get_all_items())
    elif hasattr(search, "items"):
        found = list(search.items())
    else:
        found = []

    return found[0] if found else None


@router.post("/modis14a2/query")
async def query_modis_fire(payload: ModisFireQueryRequest):
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    try:
        catalog = pystac_client.Client.open(
            CATALOG_URL,
            modifier=planetary_computer.sign_inplace,
        )

        center_lon = (west + east) / 2.0
        center_lat = (south + north) / 2.0
        datetime_steps = _build_datetime_steps(
            payload.date_start,
            payload.date_end,
            payload.step_days,
            payload.max_items,
        )

        frames: list[dict[str, Any]] = []
        counts_by_date: dict[str, int] = defaultdict(int)

        for dt in datetime_steps:
            item = _query_single_item_for_datetime(catalog, center_lon, center_lat, dt)
            if item is None:
                continue

            item_datetime = item.datetime.isoformat() if item.datetime is not None else dt.isoformat()
            acq_date = item_datetime[:10]
            assets = item.assets or {}
            tilejson_asset = assets.get("tilejson")
            rendered_preview_asset = assets.get("rendered_preview")
            firemask_asset = assets.get("FireMask")

            frames.append(
                {
                    "acq_date": acq_date,
                    "datetime": item_datetime,
                    "item_id": item.id,
                    "tilejson": getattr(tilejson_asset, "href", None),
                    "rendered_preview": getattr(rendered_preview_asset, "href", None),
                    "firemask": getattr(firemask_asset, "href", None),
                }
            )
            counts_by_date[acq_date] += 1

        timeline = sorted(counts_by_date.keys())

    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to query Planetary Computer: {exc}") from exc

    return {
        "collection": MODIS_FIRE_COLLECTION,
        "bbox": [west, south, east, north],
        "timeline": timeline,
        "counts_by_date": counts_by_date,
        "frames": frames,
        "feature_collection": {
            "type": "FeatureCollection",
            "features": [],
        },
        "source_items": len(frames),
        "returned_points": 0,
    }
