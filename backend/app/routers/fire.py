from __future__ import annotations

import asyncio
import json
import os
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import h3
import numpy as np
import rasterio
from rasterio.windows import Window
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.config import CATALOG_URL, ORS_ISOCHRONE_URL, VEHICLE_TO_ORS_PROFILE, VEHICLE_SPEED_KMH
from ..core.geo import validate_bbox, circle_polygon, bbox_from_geometry
from ..core.raster import window_for_bbox
from ..core.stac import open_catalog

# ─── Memory Safety Constants ─────────────────────────────────
MAX_CONCURRENT_RASTERS = 8
MAX_WINDOW_PIXELS = 20_000_000
MAX_H3_INPUT_PIXELS = 5_000_000
_RASTER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RASTERS)


router = APIRouter(prefix="/fire", tags=["fire"])

PRIMARY_FIRE_COLLECTION = "modis-14A1-061"
FIRE_COLLECTION_PRIORITY = ["modis-14A1-061"]


class ModisFireQueryRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: date
    date_end: date
    max_items: int = Field(default=740, ge=1, le=3650)
    step_days: int = Field(default=1, ge=1, le=31)


class ReachAreaPreviewRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    area_mode: str = Field(default="time")
    minutes: float = Field(default=30, ge=1, le=240)
    radius_km: float = Field(default=10, ge=0.5, le=500)
    vehicle: str = Field(default="driving")


def _validate_bbox(bbox: list[float]) -> tuple[float, float, float, float]:
    return validate_bbox(bbox)


def _build_datetime_steps(start: date, end: date, step_days: int, max_items: int) -> list[date]:
    steps: list[date] = []
    current = start
    while current <= end and len(steps) < max_items:
        steps.append(current)
        current = current + timedelta(days=step_days)
    return steps


def _query_single_item_for_datetime(
    catalog: pystac_client.Client,
    collection_id: str,
    lon: float,
    lat: float,
    dt: date,
) -> Any | None:
    geometry = {
        "type": "Point",
        "coordinates": [lon, lat],
    }

    search = catalog.search(
        collections=[collection_id],
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


def _query_items_for_range(
    catalog: pystac_client.Client,
    collection_id: str,
    bbox: list[float],
    start: date,
    end: date,
    limit: int,
) -> list[Any]:
    datetime_range = f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"
    search = catalog.search(
        collections=[collection_id],
        bbox=bbox,
        datetime=datetime_range,
        limit=limit,
    )

    if hasattr(search, "item_collection"):
        return list(search.item_collection())
    if hasattr(search, "get_all_items"):
        return list(search.get_all_items())
    if hasattr(search, "items"):
        return list(search.items())
    return []


def _parse_date_from_string(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Accept common STAC datetime formats by slicing date component first.
    short = text[:10]
    try:
        return date.fromisoformat(short)
    except ValueError:
        pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _item_acq_date(item: Any) -> date | None:
    if getattr(item, "datetime", None) is not None:
        return item.datetime.date()

    props = getattr(item, "properties", {}) or {}
    for key in ("datetime", "start_datetime", "end_datetime", "acquired"):
        parsed = _parse_date_from_string(props.get(key))
        if parsed is not None:
            return parsed

    return None


def _circle_polygon_geometry(lon: float, lat: float, radius_km: float, segments: int = 48) -> dict[str, Any]:
    return circle_polygon(lon, lat, radius_km, segments)


def _bbox_from_geometry(geometry: dict[str, Any]) -> list[float] | None:
    return bbox_from_geometry(geometry)


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    req = urlrequest.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


@router.post("/reach-area-preview")
async def reach_area_preview(payload: ReachAreaPreviewRequest):
    area_mode = (payload.area_mode or "time").strip().lower()
    vehicle = (payload.vehicle or "driving").strip().lower()
    if vehicle not in VEHICLE_TO_ORS_PROFILE:
        vehicle = "driving"

    if area_mode == "radius":
        geometry = _circle_polygon_geometry(payload.lon, payload.lat, payload.radius_km)
        bbox = _bbox_from_geometry(geometry)
        return {
            "source": "radius_circle",
            "mode": "radius",
            "minutes": None,
            "radius_km": float(payload.radius_km),
            "vehicle": vehicle,
            "geometry": geometry,
            "bbox": bbox,
        }

    minutes = max(1.0, float(payload.minutes))
    profile = VEHICLE_TO_ORS_PROFILE[vehicle]
    headers = {
        "Content-Type": "application/json",
    }
    ors_key = (os.getenv("OSM_ORS_API_KEY") or "").strip()
    if ors_key:
        headers["Authorization"] = ors_key

    body = {
        "locations": [[float(payload.lon), float(payload.lat)]],
        "range": [int(round(minutes * 60.0))],
        "range_type": "time",
        "smoothing": 0,
    }

    try:
        payload_json = await asyncio.to_thread(
            _post_json,
            f"{ORS_ISOCHRONE_URL.rstrip('/')}/{profile}",
            headers,
            body,
        )
        features = payload_json.get("features") or []
        if not features:
            raise ValueError("Isochrone API returned no features")

        geometry = features[0].get("geometry")
        if not isinstance(geometry, dict) or not geometry.get("coordinates"):
            raise ValueError("Isochrone geometry missing")

        bbox = _bbox_from_geometry(geometry)
        approx_radius_km = (minutes * VEHICLE_SPEED_KMH[vehicle]) / 60.0
        return {
            "source": "openrouteservice_isochrone",
            "mode": "time",
            "minutes": minutes,
            "radius_km": approx_radius_km,
            "vehicle": vehicle,
            "geometry": geometry,
            "bbox": bbox,
        }
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        approx_radius_km = (minutes * VEHICLE_SPEED_KMH[vehicle]) / 60.0
        geometry = _circle_polygon_geometry(payload.lon, payload.lat, approx_radius_km)
        bbox = _bbox_from_geometry(geometry)
        return {
            "source": "fallback_circle",
            "mode": "time",
            "minutes": minutes,
            "radius_km": approx_radius_km,
            "vehicle": vehicle,
            "geometry": geometry,
            "bbox": bbox,
        }


@router.post("/modis14a2/query")
async def query_modis_fire(payload: ModisFireQueryRequest):
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    try:
        catalog = open_catalog()

        datetime_steps = _build_datetime_steps(
            payload.date_start,
            payload.date_end,
            1,
            payload.max_items,
        )
        timeline = [dt.isoformat() for dt in datetime_steps]
        requested_days = set(timeline)

        center_lon = (west + east) / 2.0
        center_lat = (south + north) / 2.0
        frames: list[dict[str, Any]] = []
        counts_by_date: dict[str, int] = defaultdict(int)
        seen_item_ids: set[str] = set()

        for dt in datetime_steps:
            for collection_id in FIRE_COLLECTION_PRIORITY:
                try:
                    item = _query_single_item_for_datetime(
                        catalog,
                        collection_id,
                        center_lon,
                        center_lat,
                        dt,
                    )
                except Exception:
                    continue

                if item is None:
                    continue

                if item.id in seen_item_ids:
                    continue
                seen_item_ids.add(item.id)

                item_datetime = (
                    item.datetime.isoformat()
                    if getattr(item, "datetime", None) is not None
                    else f"{dt.isoformat()}T00:00:00Z"
                )
                acq_date = item_datetime[:10]
                if acq_date not in requested_days:
                    continue

                assets = item.assets or {}
                tilejson_asset = assets.get("tilejson")
                rendered_preview_asset = assets.get("rendered_preview")
                firemask_asset = assets.get("FireMask")

                frames.append(
                    {
                        "acq_date": acq_date,
                        "datetime": item_datetime,
                        "item_id": item.id,
                        "collection": collection_id,
                        "tilejson": getattr(tilejson_asset, "href", None),
                        "rendered_preview": getattr(rendered_preview_asset, "href", None),
                        "firemask": getattr(firemask_asset, "href", None),
                    }
                )
                counts_by_date[acq_date] += 1
                # One representative item per collection/day, like notebook pattern.
                break

        # Ensure daily timeline continuity even when some days have zero detections.
        for day in timeline:
            counts_by_date.setdefault(day, 0)

    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to query Planetary Computer: {exc}") from exc

    return {
        "collection": PRIMARY_FIRE_COLLECTION,
        "collections": FIRE_COLLECTION_PRIORITY,
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


# ──────────────────────────────────────────────────────────────
# Raster analysis endpoint — reads actual pixel data like fire.ipynb
# ──────────────────────────────────────────────────────────────


class RasterAnalysisRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: date
    date_end: date
    max_frames: int = Field(default=60, ge=1, le=3650)


def _window_for_bbox(ds, bbox_wgs84: list[float]) -> Window | None:
    return window_for_bbox(ds, bbox_wgs84, MAX_WINDOW_PIXELS)


def _analyze_single_raster(href: str, bbox: list[float]) -> dict[str, Any] | None:
    """Open a signed FireMask href, read pixels within bbox, return stats."""
    try:
        with rasterio.open(href) as ds:
            window = _window_for_bbox(ds, bbox)
            if window is None:
                return None

            band = ds.read(1, window=window, masked=True).astype("float32")
            arr = np.where(band.mask, np.nan, band.data)

            total_pixels = int(arr.size)
            valid_pixels = int(np.count_nonzero(~np.isnan(arr)))

            # MODIS FireMask classification
            # 3=non-fire land, 4=water, 5=cloud, 7=low, 8=nominal, 9=high
            fire_low = int(np.nansum(arr == 7))
            fire_nominal = int(np.nansum(arr == 8))
            fire_high = int(np.nansum(arr == 9))
            non_fire_land = int(np.nansum(arr == 3))
            water = int(np.nansum(arr == 4))
            cloud = int(np.nansum(arr == 5))

            total_fire = fire_low + fire_nominal + fire_high

            # Spatial analysis: find fire centroid and spread
            fire_mask = (arr >= 7) & (arr <= 9)
            fire_positions = np.argwhere(fire_mask)

            spatial = {}
            if fire_positions.size > 0:
                # Centroid in pixel coords
                cy = float(np.mean(fire_positions[:, 0]))
                cx = float(np.mean(fire_positions[:, 1]))
                # Convert to relative position (0-1) within bbox
                h, w = arr.shape
                spatial["centroid_rel"] = [cx / max(w, 1), cy / max(h, 1)]
                # Spread (std dev in pixels)
                spatial["spread_y"] = float(np.std(fire_positions[:, 0]))
                spatial["spread_x"] = float(np.std(fire_positions[:, 1]))
                # Bounding box of fire pixels (relative)
                y_min, x_min = fire_positions.min(axis=0)
                y_max, x_max = fire_positions.max(axis=0)
                spatial["fire_extent_rel"] = [
                    float(x_min / max(w, 1)), float(y_min / max(h, 1)),
                    float(x_max / max(w, 1)), float(y_max / max(h, 1)),
                ]
                # Quadrant distribution
                mid_y, mid_x = h // 2, w // 2
                spatial["quadrants"] = {
                    "NW": int(np.sum(fire_mask[:mid_y, :mid_x])),
                    "NE": int(np.sum(fire_mask[:mid_y, mid_x:])),
                    "SW": int(np.sum(fire_mask[mid_y:, :mid_x])),
                    "SE": int(np.sum(fire_mask[mid_y:, mid_x:])),
                }

            # Pixel resolution (approx area per pixel)
            transform = ds.transform
            pixel_size_x = abs(transform[0])  # degrees
            pixel_size_y = abs(transform[4])  # degrees
            # Approximate km² per pixel at the bbox center latitude
            center_lat = (bbox[1] + bbox[3]) / 2
            km_per_deg_lon = 111.32 * math.cos(math.radians(center_lat))
            km_per_deg_lat = 110.57
            pixel_area_km2 = (pixel_size_x * km_per_deg_lon) * (pixel_size_y * km_per_deg_lat)

            return {
                "total_pixels": total_pixels,
                "valid_pixels": valid_pixels,
                "raster_shape": [int(arr.shape[0]), int(arr.shape[1])],
                "pixel_area_km2": round(pixel_area_km2, 4),
                "classes": {
                    "non_fire_land": non_fire_land,
                    "water": water,
                    "cloud": cloud,
                    "fire_low_confidence": fire_low,
                    "fire_nominal_confidence": fire_nominal,
                    "fire_high_confidence": fire_high,
                },
                "total_fire_pixels": total_fire,
                "fire_area_km2": round(total_fire * pixel_area_km2, 2),
                "fire_fraction": round(total_fire / max(valid_pixels, 1), 6),
                "cloud_fraction": round(cloud / max(valid_pixels, 1), 4),
                "spatial": spatial,
            }
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/modis14a2/analyze")
async def analyze_modis_fire(payload: RasterAnalysisRequest):
    """
    Read actual MODIS FireMask raster pixels (like fire.ipynb) and return
    per-frame and aggregate statistics for the AI agent to interpret.
    """
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    bbox = [west, south, east, north]

    try:
        catalog = open_catalog()

        datetime_steps = _build_datetime_steps(
            payload.date_start,
            payload.date_end,
            1,
            payload.max_frames,
        )

        center_lon = (west + east) / 2.0
        center_lat = (south + north) / 2.0

        # Collect signed hrefs
        raster_tasks: list[dict[str, Any]] = []
        seen: set[str] = set()

        for dt in datetime_steps:
            for coll_id in FIRE_COLLECTION_PRIORITY:
                try:
                    item = _query_single_item_for_datetime(catalog, coll_id, center_lon, center_lat, dt)
                except Exception:
                    continue
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                assets = item.assets or {}
                firemask = assets.get("FireMask")
                if firemask and firemask.href:
                    acq_date = (
                        item.datetime.date().isoformat()
                        if getattr(item, "datetime", None)
                        else dt.isoformat()
                    )
                    raster_tasks.append({"acq_date": acq_date, "href": firemask.href})
                break

        if not raster_tasks:
            return {
                "status": "no_data",
                "message": "No MODIS FireMask rasters found for this bbox/date range",
                "bbox": bbox,
                "date_range": [payload.date_start.isoformat(), payload.date_end.isoformat()],
                "frames_analyzed": 0,
                "aggregate": None,
                "per_frame": [],
            }

        # Analyze each raster (IO-bound, bounded concurrency)
        per_frame: list[dict[str, Any]] = []

        async def analyze_one(task: dict[str, Any]) -> dict[str, Any]:
            async with _RASTER_SEMAPHORE:
                result = await asyncio.to_thread(_analyze_single_raster, task["href"], bbox)
            return {"acq_date": task["acq_date"], "analysis": result}

        # Process in batches to limit peak memory
        for batch_start in range(0, len(raster_tasks), MAX_CONCURRENT_RASTERS):
            batch = raster_tasks[batch_start:batch_start + MAX_CONCURRENT_RASTERS]
            results = await asyncio.gather(*[analyze_one(t) for t in batch])
            per_frame.extend(r for r in results if r["analysis"] and "error" not in r["analysis"])

        # Aggregate statistics across all frames
        aggregate = _compute_aggregate(per_frame, bbox)

        return {
            "status": "ok",
            "bbox": bbox,
            "date_range": [payload.date_start.isoformat(), payload.date_end.isoformat()],
            "frames_analyzed": len(per_frame),
            "frames_queried": len(raster_tasks),
            "aggregate": aggregate,
            "per_frame": per_frame,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc


def _compute_aggregate(per_frame: list[dict[str, Any]], bbox: list[float]) -> dict[str, Any]:
    """Compute aggregate statistics across all analyzed frames."""
    if not per_frame:
        return None

    total_fire_pixels_series = []
    fire_area_series = []
    fire_fraction_series = []
    cloud_fraction_series = []
    confidence_breakdown = {"low": 0, "nominal": 0, "high": 0}
    quadrant_totals = {"NW": 0, "NE": 0, "SW": 0, "SE": 0}
    dates_with_fire = []
    dates_without_fire = []
    pixel_area = None

    for entry in per_frame:
        a = entry["analysis"]
        if not a:
            continue

        total_fire = a.get("total_fire_pixels", 0)
        total_fire_pixels_series.append(total_fire)
        fire_area_series.append(a.get("fire_area_km2", 0))
        fire_fraction_series.append(a.get("fire_fraction", 0))
        cloud_fraction_series.append(a.get("cloud_fraction", 0))

        if pixel_area is None:
            pixel_area = a.get("pixel_area_km2", 0)

        classes = a.get("classes", {})
        confidence_breakdown["low"] += classes.get("fire_low_confidence", 0)
        confidence_breakdown["nominal"] += classes.get("fire_nominal_confidence", 0)
        confidence_breakdown["high"] += classes.get("fire_high_confidence", 0)

        spatial = a.get("spatial", {})
        quads = spatial.get("quadrants", {})
        for q in ["NW", "NE", "SW", "SE"]:
            quadrant_totals[q] += quads.get(q, 0)

        if total_fire > 0:
            dates_with_fire.append(entry["acq_date"])
        else:
            dates_without_fire.append(entry["acq_date"])

    n = len(per_frame)
    total_fire_all = sum(total_fire_pixels_series)

    # Temporal trend (linear regression slope)
    slope = 0.0
    if n >= 3:
        x = np.arange(n, dtype=float)
        y = np.array(total_fire_pixels_series, dtype=float)
        if np.std(x) > 0:
            slope = float(np.polyfit(x, y, 1)[0])

    # Peak detection
    peak_idx = int(np.argmax(total_fire_pixels_series)) if total_fire_pixels_series else 0
    peak_date = per_frame[peak_idx]["acq_date"] if per_frame else None
    peak_fire = total_fire_pixels_series[peak_idx] if total_fire_pixels_series else 0

    # Consecutive fire days
    max_consecutive = 0
    current_streak = 0
    for fp in total_fire_pixels_series:
        if fp > 0:
            current_streak += 1
            max_consecutive = max(max_consecutive, current_streak)
        else:
            current_streak = 0

    # Dominant direction
    max_quad = max(quadrant_totals, key=lambda k: quadrant_totals[k]) if quadrant_totals else "N/A"

    # Fire growth rate between consecutive frames
    growth_rates = []
    for i in range(1, len(fire_area_series)):
        prev = fire_area_series[i - 1]
        curr = fire_area_series[i]
        if prev > 0:
            growth_rates.append((curr - prev) / prev)

    avg_growth_rate = float(np.mean(growth_rates)) if growth_rates else 0.0

    return {
        "total_frames": n,
        "dates_with_fire": len(dates_with_fire),
        "dates_without_fire": len(dates_without_fire),
        "total_fire_pixels_all_frames": total_fire_all,
        "total_fire_area_km2": round(sum(fire_area_series), 2),
        "mean_fire_area_km2_per_day": round(float(np.mean(fire_area_series)), 2) if fire_area_series else 0,
        "max_fire_area_km2": round(float(np.max(fire_area_series)), 2) if fire_area_series else 0,
        "mean_fire_fraction": round(float(np.mean(fire_fraction_series)), 6) if fire_fraction_series else 0,
        "mean_cloud_fraction": round(float(np.mean(cloud_fraction_series)), 4) if cloud_fraction_series else 0,
        "pixel_area_km2": pixel_area,
        "confidence_breakdown": confidence_breakdown,
        "confidence_dominant": max(confidence_breakdown, key=lambda k: confidence_breakdown[k]),
        "quadrant_distribution": quadrant_totals,
        "dominant_quadrant": max_quad,
        "peak_date": peak_date,
        "peak_fire_pixels": peak_fire,
        "peak_fire_area_km2": round(peak_fire * (pixel_area or 0), 2),
        "temporal_trend_slope": round(slope, 3),
        "trend_direction": "increasing" if slope > 0.5 else ("decreasing" if slope < -0.5 else "stable"),
        "max_consecutive_fire_days": max_consecutive,
        "avg_daily_growth_rate": round(avg_growth_rate, 4),
        "fire_dates": dates_with_fire,
        "no_fire_dates": dates_without_fire,
    }


# ──────────────────────────────────────────────────────────────
# H3 hexagon endpoint — returns fire data as H3 cells for fast map rendering
# ──────────────────────────────────────────────────────────────


class H3FireRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: date
    date_end: date
    resolution: int | None = Field(default=None, ge=1, le=10)
    max_frames: int = Field(default=90, ge=1, le=3650)


def _auto_h3_resolution(bbox: list[float]) -> int:
    """Pick H3 resolution from bbox area. Fine detail for small areas."""
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
    return 4


def _raster_to_h3_cells(href: str, bbox: list[float], h3_res: int) -> list[dict[str, Any]]:
    """Read raster, find fire pixels, aggregate into H3 hexagons."""
    try:
        with rasterio.open(href) as ds:
            window = _window_for_bbox(ds, bbox)
            if window is None:
                return []

            band = ds.read(1, window=window, masked=True).astype("float32")
            arr = np.where(band.mask, np.nan, band.data)

            # Find fire pixels (classes 7, 8, 9)
            fire_mask = (arr >= 7) & (arr <= 9)
            fire_positions = np.argwhere(fire_mask)

            if fire_positions.size == 0:
                return []

            # Subsample if too many pixels to avoid memory exhaustion
            if len(fire_positions) > MAX_H3_INPUT_PIXELS:
                rng = np.random.default_rng(42)
                idx = rng.choice(len(fire_positions), MAX_H3_INPUT_PIXELS, replace=False)
                idx.sort()
                fire_positions = fire_positions[idx]

            # Vectorized pixel -> geo coordinate conversion
            win_transform = rasterio.windows.transform(window, ds.transform)
            rows = fire_positions[:, 0]
            cols = fire_positions[:, 1]
            xs = cols * win_transform.a + rows * win_transform.b + win_transform.c
            ys = cols * win_transform.d + rows * win_transform.e + win_transform.f

            # If raster CRS is not WGS84, transform to WGS84
            if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
                from rasterio.warp import transform as warp_transform
                xs_list, ys_list = warp_transform(ds.crs, "EPSG:4326", xs.tolist(), ys.tolist())
                xs = np.array(xs_list)
                ys = np.array(ys_list)

            fire_values = arr[rows, cols].astype(int)

            # Filter out-of-bounds
            valid = (ys >= -90) & (ys <= 90) & (xs >= -180) & (xs <= 180)
            xs, ys, fire_values = xs[valid], ys[valid], fire_values[valid]

            if len(fire_values) == 0:
                return []

            # Assign to H3 cells — list comprehension (faster than indexed loop)
            cells = [h3.latlng_to_cell(float(ys[i]), float(xs[i]), h3_res) for i in range(len(fire_values))]

            # Aggregate into hex_data
            hex_data: dict[str, dict[str, Any]] = {}
            for i, cell in enumerate(cells):
                val = int(fire_values[i])
                if cell not in hex_data:
                    hex_data[cell] = {
                        "h3": cell,
                        "fire_count": 0,
                        "high": 0,
                        "nominal": 0,
                        "low": 0,
                        "max_confidence": 0,
                    }
                hex_data[cell]["fire_count"] += 1
                if val == 9:
                    hex_data[cell]["high"] += 1
                elif val == 8:
                    hex_data[cell]["nominal"] += 1
                else:
                    hex_data[cell]["low"] += 1
                hex_data[cell]["max_confidence"] = max(hex_data[cell]["max_confidence"], val)

            # Filter out hexagons whose center falls outside the query bbox
            west, south, east, north = bbox
            result = []
            for cell_data in hex_data.values():
                center = h3.cell_to_latlng(cell_data["h3"])
                if south <= center[0] <= north and west <= center[1] <= east:
                    result.append(cell_data)
            return result

    except Exception as exc:
        print(f"H3 raster read error: {exc}")
        return []


@router.post("/modis14a2/h3")
async def query_modis_fire_h3(payload: H3FireRequest):
    """
    Returns MODIS fire data aggregated into H3 hexagons.
    Much faster for map rendering than loading full GeoTIFFs client-side.
    """
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    bbox = [west, south, east, north]
    h3_res = payload.resolution if payload.resolution is not None else _auto_h3_resolution(bbox)

    try:
        catalog = open_catalog()

        datetime_steps = _build_datetime_steps(
            payload.date_start,
            payload.date_end,
            1,
            payload.max_frames,
        )

        center_lon = (west + east) / 2.0
        center_lat = (south + north) / 2.0

        # Collect frames
        raster_tasks: list[dict[str, Any]] = []
        seen: set[str] = set()

        for dt in datetime_steps:
            for coll_id in FIRE_COLLECTION_PRIORITY:
                try:
                    item = _query_single_item_for_datetime(catalog, coll_id, center_lon, center_lat, dt)
                except Exception:
                    continue
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                assets = item.assets or {}
                firemask = assets.get("FireMask")
                if firemask and firemask.href:
                    acq_date = (
                        item.datetime.date().isoformat()
                        if getattr(item, "datetime", None)
                        else dt.isoformat()
                    )
                    raster_tasks.append({"acq_date": acq_date, "href": firemask.href})
                break

        if not raster_tasks:
            return {
                "status": "no_data",
                "bbox": bbox,
                "resolution": h3_res,
                "frames": [],
                "hexagons": [],
                "timeline": [dt.isoformat() for dt in datetime_steps],
            }

        # Process each raster into H3 cells (bounded concurrency)
        per_frame_hexes: list[dict[str, Any]] = []

        async def process_one(task: dict[str, Any]) -> dict[str, Any]:
            async with _RASTER_SEMAPHORE:
                cells = await asyncio.to_thread(_raster_to_h3_cells, task["href"], bbox, h3_res)
            return {"acq_date": task["acq_date"], "hexagons": cells}

        # Process in batches to limit peak memory
        for batch_start in range(0, len(raster_tasks), MAX_CONCURRENT_RASTERS):
            batch = raster_tasks[batch_start:batch_start + MAX_CONCURRENT_RASTERS]
            results = await asyncio.gather(*[process_one(t) for t in batch])
            per_frame_hexes.extend(r for r in results if r["hexagons"])

        # Also build aggregate H3 across all frames
        aggregate_hex: dict[str, dict[str, Any]] = {}
        for frame_result in per_frame_hexes:
            for cell_data in frame_result["hexagons"]:
                cell_id = cell_data["h3"]
                if cell_id not in aggregate_hex:
                    aggregate_hex[cell_id] = {
                        "h3": cell_id,
                        "fire_count": 0,
                        "high": 0,
                        "nominal": 0,
                        "low": 0,
                        "max_confidence": 0,
                        "days_active": 0,
                    }
                aggregate_hex[cell_id]["fire_count"] += cell_data["fire_count"]
                aggregate_hex[cell_id]["high"] += cell_data["high"]
                aggregate_hex[cell_id]["nominal"] += cell_data["nominal"]
                aggregate_hex[cell_id]["low"] += cell_data["low"]
                aggregate_hex[cell_id]["max_confidence"] = max(
                    aggregate_hex[cell_id]["max_confidence"], cell_data["max_confidence"]
                )
                aggregate_hex[cell_id]["days_active"] += 1

        # Add boundary coordinates for each hex (so frontend doesn't need h3-js)
        for cell_data in aggregate_hex.values():
            boundary = h3.cell_to_boundary(cell_data["h3"])
            # boundary is list of (lat, lng) tuples
            cell_data["boundary"] = [[lat, lng] for lat, lng in boundary]
            center = h3.cell_to_latlng(cell_data["h3"])
            cell_data["center"] = [center[0], center[1]]

        # Per-frame hex data: only include cell ID and counts (no boundaries)
        # Frontend can compute boundaries from h3 IDs if needed per-frame
        for frame_result in per_frame_hexes:
            for cell_data in frame_result["hexagons"]:
                cell_data.pop("boundary", None)
                cell_data.pop("center", None)

        return {
            "status": "ok",
            "bbox": bbox,
            "resolution": h3_res,
            "timeline": [dt.isoformat() for dt in datetime_steps],
            "frames": per_frame_hexes,
            "hexagons": list(aggregate_hex.values()),
            "total_hexagons": len(aggregate_hex),
            "frames_with_fire": len(per_frame_hexes),
            "frames_queried": len(raster_tasks),
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"H3 analysis failed: {exc}") from exc
