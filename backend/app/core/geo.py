"""Shared geographic utility functions."""
from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException


def validate_bbox(bbox: list[float]) -> tuple[float, float, float, float]:
    """Validate and unpack a WGS84 bounding box."""
    west, south, east, north = [float(v) for v in bbox]
    if west >= east or south >= north:
        raise HTTPException(status_code=400, detail="Invalid bbox coordinates")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise HTTPException(status_code=400, detail="bbox out of WGS84 range")
    return west, south, east, north


def auto_h3_resolution(bbox: list[float]) -> int:
    """Pick H3 resolution from bbox span. Fine detail for small areas."""
    west, south, east, north = bbox
    span = max(abs(east - west), abs(north - south))
    if span < 0.02:
        return 10
    if span < 0.1:
        return 9
    if span < 0.3:
        return 8
    if span < 1.0:
        return 7
    if span < 3.0:
        return 6
    if span < 8.0:
        return 5
    if span < 20.0:
        return 4
    if span < 60.0:
        return 3
    if span < 120.0:
        return 2
    return 1


def circle_polygon(
    lon: float, lat: float, radius_km: float, segments: int = 48
) -> dict[str, Any]:
    """Create a GeoJSON Polygon approximating a circle."""
    radius = max(0.1, float(radius_km))
    lat_delta = radius / 111.0
    cos_lat = max(0.1, abs(math.cos(math.radians(float(lat)))))
    lon_delta = radius / (111.0 * cos_lat)

    ring: list[list[float]] = []
    for idx in range(segments):
        angle = (2.0 * math.pi * idx) / segments
        ring.append([
            float(lon) + lon_delta * math.cos(angle),
            float(lat) + lat_delta * math.sin(angle),
        ])
    if ring:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def bbox_from_geometry(geometry: dict[str, Any]) -> list[float] | None:
    """Extract a [west, south, east, north] bbox from any GeoJSON geometry."""
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return None
    lons: list[float] = []
    lats: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if len(node) == 2 and all(isinstance(v, (int, float)) for v in node):
                lons.append(float(node[0]))
                lats.append(float(node[1]))
                return
            for child in node:
                walk(child)

    walk(coords)
    if not lons or not lats:
        return None
    return [min(lons), min(lats), max(lons), max(lats)]
