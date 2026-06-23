"""
Multi-dataset H3 endpoint — supports fire, flood, vegetation, weather, land use.
Reads rasters from Planetary Computer STAC, converts pixels to H3 hexagons.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import h3
import numpy as np
import planetary_computer
import pystac_client
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ─── Memory Safety Constants ─────────────────────────────────
MAX_CONCURRENT_RASTERS = 8          # max simultaneous raster reads
MAX_WINDOW_PIXELS = 50_000_000      # cap raster window at ~50M pixels
MAX_H3_INPUT_PIXELS = 10_000_000    # max pixels to convert to H3
_RASTER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RASTERS)

# ─── STAC Item Cache (avoid re-querying same collection/bbox/date range)
_stac_cache: dict[str, list] = {}
_STAC_CACHE_MAX = 100

def _stac_cache_key(collection: str, bbox: list[float], dt_start: str, dt_end: str) -> str:
    bk = ",".join(f"{v:.2f}" for v in bbox)
    return f"{collection}|{bk}|{dt_start}|{dt_end}"

# GDAL/rasterio environment for faster COG reads
_GDAL_ENV = {
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "5000000",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
}

router = APIRouter(prefix="/datasets", tags=["datasets"])

env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(env_path)

CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# ─── Dataset Configurations ───────────────────────────────────

DATASET_CONFIG = {
    "fire": {
        "collection": "modis-14A1-061",
        "asset": "FireMask",
        "description": "MODIS Daily Fire (14A1)",
        "value_type": "classification",
        # Fire classes: 7=low, 8=nominal, 9=high confidence
        "filter": lambda arr: (arr >= 7) & (arr <= 9),
        "classify": lambda val: (
            "high" if val >= 9 else "nominal" if val >= 8 else "low"
        ),
        "scale": None,
        "max_h3_res": 7,  # MODIS ~1km pixels
    },
    "flood": {
        "collection": "jrc-gsw",
        "asset": "occurrence",
        "description": "JRC Global Surface Water",
        "value_type": "continuous",
        # Water occurrence 0-100 (% of time water present)
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "permanent" if val >= 80 else "seasonal" if val >= 30 else "ephemeral"
        ),
        "scale": None,
        "max_h3_res": 9,  # JRC ~30m pixels
    },
    "vegetation": {
        "collection": "modis-13A1-061",
        "asset": "500m_16_days_NDVI",
        "description": "MODIS NDVI (16-day)",
        "value_type": "continuous",
        # NDVI raw values; scale by /10000 to get -1 to 1
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "dense" if val > 6000 else "moderate" if val > 3000 else "sparse" if val > 1000 else "bare"
        ),
        "scale": 10000.0,  # divide by this to get real NDVI
        "max_h3_res": 7,  # MODIS ~500m pixels
    },
    "weather": {
        "collection": "modis-11A2-061",
        "asset": "LST_Day_1km",
        "description": "MODIS Land Surface Temperature",
        "value_type": "continuous",
        # LST scale: raw * 0.02 = Kelvin
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "extreme" if val > 16000 else "hot" if val > 15000 else "warm" if val > 14000 else "cool"
        ),
        "scale": None,  # Will convert: raw * 0.02 - 273.15 = °C
        "max_h3_res": 7,  # MODIS ~1km pixels
    },
    "landuse": {
        "collection": "esa-worldcover",
        "asset": "map",
        "description": "ESA WorldCover (10m)",
        "value_type": "classification",
        # Classes: 10=tree, 20=shrub, 30=grass, 40=crop, 50=built, 60=bare, 80=water
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "tree" if val == 10 else "shrub" if val == 20 else "grass" if val == 30
            else "crop" if val == 40 else "built" if val == 50 else "bare" if val == 60
            else "water" if val == 80 else "other"
        ),
        "scale": None,
        "max_h3_res": 9,  # ESA ~10m pixels
    },
}


# ─── Request Model ────────────────────────────────────────────

class DatasetH3Request(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    date_start: date
    date_end: date
    datasets: list[str] = Field(default=["fire"])
    resolution: int | None = Field(default=None, ge=1, le=12)
    max_frames: int = Field(default=365, ge=1, le=3650)


# ─── Helpers ──────────────────────────────────────────────────

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

def _validate_bbox(bbox: list[float]) -> tuple[float, float, float, float]:
    west, south, east, north = [float(v) for v in bbox]
    if west >= east or south >= north:
        raise HTTPException(status_code=400, detail="Invalid bbox")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise HTTPException(status_code=400, detail="bbox out of WGS84 range")
    return west, south, east, north


def _build_datetime_steps(start: date, end: date, max_items: int) -> list[date]:
    """Build date steps, sampling evenly if the range exceeds max_items."""
    total_days = (end - start).days + 1
    if total_days <= 0:
        return [start]
    if total_days <= max_items:
        # Every day fits
        return [start + timedelta(days=i) for i in range(total_days)]
    # Sample evenly across the range
    step = total_days / max_items
    return [start + timedelta(days=int(i * step)) for i in range(max_items)]


def _item_acq_date(item, fallback: date) -> str:
    """Extract acquisition date from a STAC item, handling various datetime fields."""
    if getattr(item, "datetime", None):
        return item.datetime.date().isoformat()
    # MODIS and other products use start_datetime in properties
    start_dt = (item.properties or {}).get("start_datetime")
    if start_dt:
        return start_dt[:10]  # "2024-05-26T00:00:00Z" → "2024-05-26"
    return fallback.isoformat()


def _window_for_bbox(ds, bbox_wgs84: list[float]):
    west, south, east, north = bbox_wgs84
    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
    )
    candidate = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
    bounded = candidate.intersection(Window(0, 0, ds.width, ds.height))
    if bounded.width <= 0 or bounded.height <= 0:
        return None
    bounded = bounded.round_offsets().round_lengths()
    # Cap window size to avoid loading huge arrays into memory
    total_px = bounded.width * bounded.height
    if total_px > MAX_WINDOW_PIXELS:
        scale = math.sqrt(MAX_WINDOW_PIXELS / total_px)
        new_w = max(1, int(bounded.width * scale))
        new_h = max(1, int(bounded.height * scale))
        bounded = Window(
            col_off=bounded.col_off,
            row_off=bounded.row_off,
            width=new_w,
            height=new_h,
        )
    return bounded


def _query_items_bbox(catalog, collection: str, bbox: list[float], dt: date, max_items: int = 10) -> list:
    """Query STAC items that intersect the bbox for a given date. Uses cache."""
    cache_key = _stac_cache_key(collection, bbox, dt.isoformat(), dt.isoformat())
    if cache_key in _stac_cache:
        return _stac_cache[cache_key]

    west, south, east, north = bbox
    search = catalog.search(
        collections=[collection],
        bbox=[west, south, east, north],
        datetime=dt.isoformat(),
        limit=max_items,
    )
    if hasattr(search, "item_collection"):
        items = list(search.item_collection())
    elif hasattr(search, "get_all_items"):
        items = list(search.get_all_items())
    else:
        items = list(search.items()) if hasattr(search, "items") else []

    _stac_cache[cache_key] = items
    if len(_stac_cache) > _STAC_CACHE_MAX:
        oldest = next(iter(_stac_cache))
        del _stac_cache[oldest]
    return items


def _query_items_bbox_daterange(catalog, collection: str, bbox: list[float],
                                dt_start: date, dt_end: date, max_items: int = 200) -> list:
    """Query STAC items for an entire date range in a single request."""
    cache_key = _stac_cache_key(collection, bbox, dt_start.isoformat(), dt_end.isoformat())
    if cache_key in _stac_cache:
        return _stac_cache[cache_key]

    west, south, east, north = bbox
    dt_str = f"{dt_start.isoformat()}/{dt_end.isoformat()}"
    search = catalog.search(
        collections=[collection],
        bbox=[west, south, east, north],
        datetime=dt_str,
        limit=max_items,
    )
    if hasattr(search, "item_collection"):
        items = list(search.item_collection())
    elif hasattr(search, "get_all_items"):
        items = list(search.get_all_items())
    else:
        items = list(search.items()) if hasattr(search, "items") else []

    _stac_cache[cache_key] = items
    if len(_stac_cache) > _STAC_CACHE_MAX:
        oldest = next(iter(_stac_cache))
        del _stac_cache[oldest]
    return items


def _query_items_bbox_no_date(catalog, collection: str, bbox: list[float], max_items: int = 10) -> list:
    """Query STAC items that intersect the bbox (no date constraint). Uses cache."""
    cache_key = _stac_cache_key(collection, bbox, "none", "none")
    if cache_key in _stac_cache:
        return _stac_cache[cache_key]

    west, south, east, north = bbox
    search = catalog.search(
        collections=[collection],
        bbox=[west, south, east, north],
        limit=max_items,
    )
    if hasattr(search, "item_collection"):
        items = list(search.item_collection())
    elif hasattr(search, "get_all_items"):
        items = list(search.get_all_items())
    else:
        items = list(search.items()) if hasattr(search, "items") else []

    _stac_cache[cache_key] = items
    if len(_stac_cache) > _STAC_CACHE_MAX:
        oldest = next(iter(_stac_cache))
        del _stac_cache[oldest]
    return items


def _overview_factor(ds, window, h3_res: int) -> int | None:
    """Pick a COG overview level that matches the target H3 resolution.
    Returns a decimation factor, or None to read at full resolution."""
    if not ds.overviews(1):
        return None
    # Approximate H3 cell edge length in degrees at equator
    # res 3 ≈ 1.2°, res 4 ≈ 0.5°, res 5 ≈ 0.2°, res 6 ≈ 0.07°, res 7+ ≈ full
    h3_approx_deg = {3: 1.2, 4: 0.5, 5: 0.2, 6: 0.07}
    target_deg = h3_approx_deg.get(h3_res)
    if target_deg is None:
        return None  # res 7+ → read full resolution
    # Current pixel size in degrees
    px_deg = abs(ds.transform.a)
    if px_deg <= 0:
        return None
    ideal_factor = target_deg / px_deg
    if ideal_factor < 2:
        return None
    # Pick the closest overview that doesn't overshoot
    overviews = sorted(ds.overviews(1))
    for ov in overviews:
        if ov >= ideal_factor * 0.5:
            return ov
    return overviews[-1] if overviews else None


def _raster_to_h3(
    href: str,
    bbox: list[float],
    h3_res: int,
    dataset_key: str,
) -> list[dict[str, Any]]:
    """Read raster, find matching pixels, aggregate into H3 hexagons."""
    config = DATASET_CONFIG[dataset_key]
    filter_fn = config["filter"]
    classify_fn = config["classify"]

    try:
        with rasterio.Env(**_GDAL_ENV):
          with rasterio.open(href) as ds:
            window = _window_for_bbox(ds, bbox)
            if window is None:
                return []

            # Use COG overviews for coarse H3 resolutions — read far less data
            ov_factor = _overview_factor(ds, window, h3_res)
            if ov_factor and ov_factor >= 2:
                out_h = max(1, int(window.height / ov_factor))
                out_w = max(1, int(window.width / ov_factor))
                band = ds.read(1, window=window, out_shape=(out_h, out_w),
                               masked=True, resampling=rasterio.enums.Resampling.nearest
                               ).astype("float32")
                # Adjust transform for the downsampled grid
                win_transform = rasterio.windows.transform(window, ds.transform)
                win_transform = rasterio.Affine(
                    win_transform.a * (window.width / out_w),
                    win_transform.b,
                    win_transform.c,
                    win_transform.d,
                    win_transform.e * (window.height / out_h),
                    win_transform.f,
                )
            else:
                band = ds.read(1, window=window, masked=True).astype("float32")
                win_transform = rasterio.windows.transform(window, ds.transform)

            arr = np.where(band.mask, np.nan, band.data)

            # Apply dataset-specific filter
            valid_mask = ~np.isnan(arr)
            data_mask = np.zeros_like(arr, dtype=bool)
            data_mask[valid_mask] = filter_fn(arr[valid_mask])

            positions = np.argwhere(data_mask)
            if positions.size == 0:
                return []

            # Subsample if too many pixels to avoid memory exhaustion
            if len(positions) > MAX_H3_INPUT_PIXELS:
                rng = np.random.default_rng(42)
                idx = rng.choice(len(positions), MAX_H3_INPUT_PIXELS, replace=False)
                idx.sort()
                positions = positions[idx]

            # Pixel → geographic coordinates (vectorized)
            rows = positions[:, 0]
            cols = positions[:, 1]
            xs = cols * win_transform.a + rows * win_transform.b + win_transform.c
            ys = cols * win_transform.d + rows * win_transform.e + win_transform.f

            # Transform to WGS84 if needed
            if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
                from rasterio.warp import transform as warp_transform
                xs_list, ys_list = warp_transform(ds.crs, "EPSG:4326", xs.tolist(), ys.tolist())
                xs = np.array(xs_list)
                ys = np.array(ys_list)

            values = arr[rows, cols]

            # Filter out-of-bounds coords with numpy
            valid = (ys >= -90) & (ys <= 90) & (xs >= -180) & (xs <= 180) & ~np.isnan(values)
            xs, ys, values = xs[valid], ys[valid], values[valid]

            if len(values) == 0:
                return []

            # Assign pixels to H3 cells — batch via list comprehension (faster than indexed loop)
            cells = [h3.latlng_to_cell(float(ys[i]), float(xs[i]), h3_res) for i in range(len(values))]

            # Aggregate into hex_data
            hex_data: dict[str, dict[str, Any]] = {}
            for i, cell in enumerate(cells):
                val = float(values[i])
                cls = classify_fn(val)

                if cell not in hex_data:
                    hex_data[cell] = {
                        "h3": cell,
                        "pixel_count": 0,
                        "value_sum": 0.0,
                        "value_max": -1e9,
                        "value_min": 1e9,
                        "classes": defaultdict(int),
                    }

                hex_data[cell]["pixel_count"] += 1
                hex_data[cell]["value_sum"] += val
                hex_data[cell]["value_max"] = max(hex_data[cell]["value_max"], val)
                hex_data[cell]["value_min"] = min(hex_data[cell]["value_min"], val)
                hex_data[cell]["classes"][cls] += 1

            # Finalize — compute boundaries and stats
            result = []
            for cell_id, data in hex_data.items():
                n = data["pixel_count"]
                boundary = h3.cell_to_boundary(cell_id)
                center = h3.cell_to_latlng(cell_id)
                classes = dict(data["classes"])
                dominant = max(classes, key=classes.get) if classes else "unknown"

                result.append({
                    "h3": cell_id,
                    "pixel_count": n,
                    "value_mean": round(data["value_sum"] / max(n, 1), 2),
                    "value_max": round(data["value_max"], 2),
                    "value_min": round(data["value_min"], 2),
                    "dominant_class": dominant,
                    "classes": classes,
                    "boundary": [[lat, lng] for lat, lng in boundary],
                    "center": [center[0], center[1]],
                })

            return result

    except Exception as exc:
        print(f"Raster H3 error ({dataset_key}): {exc}")
        return []


# ─── Main Endpoint ────────────────────────────────────────────

@router.post("/h3")
async def query_datasets_h3(payload: DatasetH3Request):
    """
    Multi-dataset H3 query. Reads rasters for each requested dataset,
    converts pixels to H3 hexagons, returns per-dataset and per-day data.
    """
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    bbox = [west, south, east, north]
    h3_res = payload.resolution if payload.resolution is not None else _auto_h3_resolution(bbox)

    # Validate requested datasets
    valid_datasets = [d for d in payload.datasets if d in DATASET_CONFIG]
    if not valid_datasets:
        raise HTTPException(status_code=400, detail=f"No valid datasets. Available: {list(DATASET_CONFIG.keys())}")

    try:
        catalog = pystac_client.Client.open(
            CATALOG_URL,
            modifier=planetary_computer.sign_inplace,
        )

        datetime_steps = _build_datetime_steps(
            payload.date_start, payload.date_end, payload.max_frames
        )
        timeline = [dt.isoformat() for dt in datetime_steps]

        results: dict[str, Any] = {}

        for dataset_key in valid_datasets:
            config = DATASET_CONFIG[dataset_key]
            collection = config["collection"]
            asset_name = config["asset"]
            is_static = dataset_key in ("landuse", "flood")  # No temporal dimension

            # Cap resolution to what the source data can support
            ds_h3_res = min(h3_res, config.get("max_h3_res", h3_res))

            dataset_frames: list[dict[str, Any]] = []
            aggregate_hex: dict[str, dict[str, Any]] = {}
            seen_items: set[str] = set()

            def _get_asset(item, ds_key=dataset_key, a_name=asset_name):
                """Get the raster asset from a STAC item, with fallbacks."""
                asset = (item.assets or {}).get(a_name)
                if not asset:
                    alt_names = {
                        "fire": ["FireMask"],
                        "vegetation": ["500m_16_days_NDVI", "NDVI"],
                        "weather": ["LST_Day_1km", "LST_Night_1km", "LST_Day_6km"],
                    }
                    for alt in alt_names.get(ds_key, []):
                        if alt in (item.assets or {}):
                            return item.assets[alt]
                    # Last resort: first non-metadata asset
                    for k, v in (item.assets or {}).items():
                        if k not in ("tilejson", "rendered_preview", "metadata"):
                            return v
                return asset

            if is_static:
                # Static datasets: query ALL tiles covering the bbox
                items = await asyncio.to_thread(
                    _query_items_bbox_no_date, catalog, collection, bbox
                )
                for item in items:
                    if item.id in seen_items:
                        continue
                    seen_items.add(item.id)
                    asset = _get_asset(item)
                    if not asset or not asset.href:
                        continue
                    hexes = await asyncio.to_thread(
                        _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                    )
                    if hexes:
                        dataset_frames.append({"acq_date": "static", "hexagons": hexes})
                        for h in hexes:
                            aggregate_hex[h["h3"]] = h
            else:
                # Single STAC query for the entire date range (instead of per-day)
                all_items = await asyncio.to_thread(
                    _query_items_bbox_daterange, catalog, collection, bbox,
                    payload.date_start, payload.date_end, 200
                )

                # Group items by date for frame organization
                items_by_date: dict[str, list] = {}
                for item in all_items:
                    if item.id in seen_items:
                        continue
                    seen_items.add(item.id)
                    acq_date = _item_acq_date(item, payload.date_start)
                    items_by_date.setdefault(acq_date, []).append(item)

                # Process all items concurrently (semaphore limits raster I/O)
                async def process_item(item, acq_date: str) -> dict[str, Any] | None:
                    asset = _get_asset(item)
                    if not asset or not asset.href:
                        return None
                    async with _RASTER_SEMAPHORE:
                        hexes = await asyncio.to_thread(
                            _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                        )
                    if hexes:
                        return {"acq_date": acq_date, "hexagons": hexes}
                    return None

                tasks = []
                for acq_date, items in items_by_date.items():
                    for item in items:
                        tasks.append(process_item(item, acq_date))

                results_list = await asyncio.gather(*tasks)

                for frame in results_list:
                    if frame and frame["hexagons"]:
                            dataset_frames.append(frame)
                            for h in frame["hexagons"]:
                                cell_id = h["h3"]
                                if cell_id not in aggregate_hex:
                                    aggregate_hex[cell_id] = {
                                        **h,
                                        "days_active": 0,
                                        "total_pixels": 0,
                                        "_value_sum": 0.0,
                                        "_value_count": 0,
                                    }
                                agg = aggregate_hex[cell_id]
                                agg["days_active"] = agg.get("days_active", 0) + 1
                                agg["total_pixels"] = agg.get("total_pixels", 0) + h["pixel_count"]
                                # Accumulate for proper mean
                                agg["_value_sum"] = agg.get("_value_sum", 0.0) + h["value_mean"] * h["pixel_count"]
                                agg["_value_count"] = agg.get("_value_count", 0) + h["pixel_count"]
                                # Keep max values
                                if h["value_max"] > agg["value_max"]:
                                    agg["value_max"] = h["value_max"]
                                # Merge classes
                                for cls, cnt in h["classes"].items():
                                    agg["classes"][cls] = agg["classes"].get(cls, 0) + cnt
                                # Update dominant
                                agg["dominant_class"] = max(agg["classes"], key=agg["classes"].get)

            # Finalize aggregate value_mean
            for cell_id, agg in aggregate_hex.items():
                if agg.get("_value_count", 0) > 0:
                    agg["value_mean"] = round(agg["_value_sum"] / agg["_value_count"], 2)
                agg.pop("_value_sum", None)
                agg.pop("_value_count", None)

            results[dataset_key] = {
                "dataset": dataset_key,
                "collection": config["collection"],
                "description": config["description"],
                "frames": dataset_frames,
                "hexagons": list(aggregate_hex.values()),
                "total_hexagons": len(aggregate_hex),
                "frames_with_data": len(dataset_frames),
            }

        return {
            "status": "ok",
            "bbox": bbox,
            "resolution": h3_res,
            "date_range": [payload.date_start.isoformat(), payload.date_end.isoformat()],
            "timeline": timeline,
            "datasets": results,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dataset query failed: {exc}") from exc


# ─── Streaming Endpoint (NDJSON — one line per dataset) ───────

@router.post("/h3/stream")
async def query_datasets_h3_stream(payload: DatasetH3Request):
    """
    Streaming version: sends each dataset result as a separate NDJSON line
    as soon as it's ready, so the frontend can render progressively.
    """
    west, south, east, north = _validate_bbox(payload.bbox)
    if payload.date_start > payload.date_end:
        raise HTTPException(status_code=400, detail="date_start must be before date_end")

    bbox = [west, south, east, north]
    h3_res = payload.resolution if payload.resolution is not None else _auto_h3_resolution(bbox)

    valid_datasets = [d for d in payload.datasets if d in DATASET_CONFIG]
    if not valid_datasets:
        raise HTTPException(status_code=400, detail=f"No valid datasets. Available: {list(DATASET_CONFIG.keys())}")

    async def generate():
        try:
            catalog = pystac_client.Client.open(
                CATALOG_URL,
                modifier=planetary_computer.sign_inplace,
            )
            datetime_steps = _build_datetime_steps(
                payload.date_start, payload.date_end, payload.max_frames
            )
            timeline = [dt.isoformat() for dt in datetime_steps]

            # Send header line with metadata
            header = {
                "type": "header",
                "bbox": bbox,
                "resolution": h3_res,
                "date_range": [payload.date_start.isoformat(), payload.date_end.isoformat()],
                "timeline": timeline,
                "total_datasets": len(valid_datasets),
            }
            yield json.dumps(header) + "\n"

            # Process all datasets concurrently, stream frames as they arrive
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def process_one_dataset(dataset_key: str) -> None:
                config = DATASET_CONFIG[dataset_key]
                collection = config["collection"]
                asset_name = config["asset"]
                is_static = dataset_key in ("landuse", "flood")
                ds_h3_res = min(h3_res, config.get("max_h3_res", h3_res))

                frames_count = 0
                aggregate_hex: dict[str, dict[str, Any]] = {}
                seen_items: set[str] = set()

                def _get_asset(item, ds_key=dataset_key, a_name=asset_name):
                    asset = (item.assets or {}).get(a_name)
                    if not asset:
                        alt_names = {
                            "fire": ["FireMask"],
                            "vegetation": ["500m_16_days_NDVI", "NDVI"],
                            "weather": ["LST_Day_1km", "LST_Night_1km", "LST_Day_6km"],
                        }
                        for alt in alt_names.get(ds_key, []):
                            if alt in (item.assets or {}):
                                return item.assets[alt]
                        for k, v in (item.assets or {}).items():
                            if k not in ("tilejson", "rendered_preview", "metadata"):
                                return v
                    return asset

                def _accumulate_aggregate(hexes):
                    for h in hexes:
                        cell_id = h["h3"]
                        if cell_id not in aggregate_hex:
                            aggregate_hex[cell_id] = {
                                **h,
                                "days_active": 0,
                                "total_pixels": 0,
                                "_value_sum": 0.0,
                                "_value_count": 0,
                            }
                        agg = aggregate_hex[cell_id]
                        agg["days_active"] = agg.get("days_active", 0) + 1
                        agg["total_pixels"] = agg.get("total_pixels", 0) + h["pixel_count"]
                        agg["_value_sum"] = agg.get("_value_sum", 0.0) + h["value_mean"] * h["pixel_count"]
                        agg["_value_count"] = agg.get("_value_count", 0) + h["pixel_count"]
                        if h["value_max"] > agg["value_max"]:
                            agg["value_max"] = h["value_max"]
                        for cls, cnt in h["classes"].items():
                            agg["classes"][cls] = agg["classes"].get(cls, 0) + cnt
                        agg["dominant_class"] = max(agg["classes"], key=agg["classes"].get)

                if is_static:
                    items = await asyncio.to_thread(
                        _query_items_bbox_no_date, catalog, collection, bbox
                    )
                    for item in items:
                        if item.id in seen_items:
                            continue
                        seen_items.add(item.id)
                        asset = _get_asset(item)
                        if not asset or not asset.href:
                            continue
                        async with _RASTER_SEMAPHORE:
                            hexes = await asyncio.to_thread(
                                _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                            )
                        if hexes:
                            # Get date from item if available
                            acq_date = _item_acq_date(item, payload.date_start)
                            frame_msg = {
                                "type": "frame",
                                "key": dataset_key,
                                "frame": {"acq_date": acq_date, "hexagons": hexes},
                            }
                            await queue.put(json.dumps(frame_msg) + "\n")
                            frames_count += 1
                            for h in hexes:
                                aggregate_hex[h["h3"]] = h
                else:
                    # Single STAC query for the entire date range
                    all_items = await asyncio.to_thread(
                        _query_items_bbox_daterange, catalog, collection, bbox,
                        payload.date_start, payload.date_end, 200
                    )

                    # Group items by date for frame organization
                    items_by_date: dict[str, list] = {}
                    for item in all_items:
                        if item.id in seen_items:
                            continue
                        seen_items.add(item.id)
                        acq_date = _item_acq_date(item, payload.date_start)
                        items_by_date.setdefault(acq_date, []).append(item)

                    # Process each date's items and stream frames immediately
                    sorted_dates = sorted(items_by_date.keys())
                    for acq_date in sorted_dates:
                        date_items = items_by_date[acq_date]

                        async def process_item(item) -> list[dict[str, Any]]:
                            asset = _get_asset(item)
                            if not asset or not asset.href:
                                return []
                            async with _RASTER_SEMAPHORE:
                                return await asyncio.to_thread(
                                    _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                                )

                        # Process items for this date concurrently
                        results_list = await asyncio.gather(
                            *(process_item(item) for item in date_items)
                        )

                        # Merge all hexagons for this date
                        day_hexagons: dict[str, dict[str, Any]] = {}
                        for hexes in results_list:
                            for h in hexes:
                                day_hexagons[h["h3"]] = h

                        if day_hexagons:
                            merged_hexes = list(day_hexagons.values())
                            frame_msg = {
                                "type": "frame",
                                "key": dataset_key,
                                "frame": {"acq_date": acq_date, "hexagons": merged_hexes},
                            }
                            await queue.put(json.dumps(frame_msg) + "\n")
                            frames_count += 1
                            _accumulate_aggregate(merged_hexes)

                # Finalize aggregate value_mean
                for cell_id, agg in aggregate_hex.items():
                    if agg.get("_value_count", 0) > 0:
                        agg["value_mean"] = round(agg["_value_sum"] / agg["_value_count"], 2)
                    agg.pop("_value_sum", None)
                    agg.pop("_value_count", None)

                # Signal dataset complete
                done_msg = {
                    "type": "dataset_done",
                    "key": dataset_key,
                    "data": {
                        "dataset": dataset_key,
                        "collection": config["collection"],
                        "description": config["description"],
                        "hexagons": list(aggregate_hex.values()),
                        "total_hexagons": len(aggregate_hex),
                        "frames_with_data": frames_count,
                    },
                }
                await queue.put(json.dumps(done_msg) + "\n")

            # Launch all datasets concurrently
            async def run_all():
                await asyncio.gather(
                    *(process_one_dataset(dk) for dk in valid_datasets),
                    return_exceptions=True,
                )
                await queue.put(None)  # sentinel

            task = asyncio.ensure_future(run_all())

            # Yield results as they arrive
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item

            await task  # ensure exceptions propagate

            # Send done signal
            yield json.dumps({"type": "done"}) + "\n"

        except Exception as exc:
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ─── Isochrone / Reach Area ───────────────────────────────────

ORS_ISOCHRONE_URL = os.getenv(
    "OSM_ORS_ISOCHRONE_URL",
    "https://api.openrouteservice.org/v2/isochrones",
)
VEHICLE_TO_ORS_PROFILE = {
    "walking": "foot-walking",
    "cycling": "cycling-regular",
    "driving": "driving-car",
    "driving-hgv": "driving-hgv",
}
VEHICLE_SPEED_KMH = {
    "walking": 5.0,
    "cycling": 15.0,
    "driving": 60.0,
    "driving-hgv": 45.0,
}


class ReachAreaRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    area_mode: str = Field(default="time")
    minutes: float = Field(default=30, ge=1, le=240)
    radius_km: float = Field(default=10, ge=0.5, le=500)
    vehicle: str = Field(default="driving")


def _circle_polygon(lon: float, lat: float, radius_km: float, segments: int = 48) -> dict[str, Any]:
    radius = max(0.1, float(radius_km))
    lat_delta = radius / 111.0
    cos_lat = max(0.1, abs(math.cos(math.radians(float(lat)))))
    lon_delta = radius / (111.0 * cos_lat)

    ring: list[list[float]] = []
    for idx in range(segments):
        angle = (2.0 * math.pi * idx) / segments
        ring.append([
            float(lon) + (lon_delta * math.cos(angle)),
            float(lat) + (lat_delta * math.sin(angle)),
        ])
    if ring:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _bbox_from_geom(geometry: dict[str, Any]) -> list[float] | None:
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


def _post_json_req(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    req = urlrequest.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


@router.post("/reach-area")
async def reach_area(payload: ReachAreaRequest):
    """
    Compute isochrone or radius reach area for any dataset analysis.
    Returns a geometry polygon and bbox that can be used to query any dataset.
    """
    area_mode = (payload.area_mode or "time").strip().lower()
    vehicle = (payload.vehicle or "driving").strip().lower()
    if vehicle not in VEHICLE_TO_ORS_PROFILE:
        vehicle = "driving"

    if area_mode == "radius":
        geometry = _circle_polygon(payload.lon, payload.lat, payload.radius_km)
        bbox = _bbox_from_geom(geometry)
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
    headers = {"Content-Type": "application/json"}
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
        resp_json = await asyncio.to_thread(
            _post_json_req,
            f"{ORS_ISOCHRONE_URL.rstrip('/')}/{profile}",
            headers,
            body,
        )
        features = resp_json.get("features") or []
        if not features:
            raise ValueError("Isochrone API returned no features")

        geometry = features[0].get("geometry")
        if not isinstance(geometry, dict) or not geometry.get("coordinates"):
            raise ValueError("Isochrone geometry missing")

        bbox = _bbox_from_geom(geometry)
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
        geometry = _circle_polygon(payload.lon, payload.lat, approx_radius_km)
        bbox = _bbox_from_geom(geometry)
        return {
            "source": "fallback_circle",
            "mode": "time",
            "minutes": minutes,
            "radius_km": approx_radius_km,
            "vehicle": vehicle,
            "geometry": geometry,
            "bbox": bbox,
        }


# ─── Geocoding ────────────────────────────────────────────────

ESRI_GEOCODE_URL = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_geocode_cache: dict[str, dict[str, Any]] = {}


class GeocodeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


@router.post("/geocode")
async def geocode_place(payload: GeocodeRequest):
    """Resolve a place name to coordinates and bbox using ESRI (primary) with Nominatim fallback."""
    query = payload.query.strip()
    cache_key = query.lower()

    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # --- Try ESRI first (fast, snappy) ---
    result = await _geocode_esri(query)

    # --- Fallback to Nominatim ---
    if result is None:
        result = await _geocode_nominatim(query)

    if result is None:
        raise HTTPException(status_code=404, detail=f"Place not found: {query}")

    _geocode_cache[cache_key] = result
    return result


async def _geocode_esri(query: str) -> dict[str, Any] | None:
    """Try ESRI ArcGIS World Geocoder."""
    from urllib.parse import urlencode

    esri_key = os.getenv("ESRI_API_KEY", "")
    params: dict[str, str] = {
        "f": "json",
        "singleLine": query,
        "maxLocations": "1",
        "outFields": "*",
    }
    if esri_key:
        params["token"] = esri_key
    url = f"{ESRI_GEOCODE_URL}?{urlencode(params)}"
    req = urlrequest.Request(url, headers={"User-Agent": "EarthAnalyser/1.0"})

    try:
        raw = await asyncio.to_thread(_fetch_url, req)
        data = json.loads(raw)
    except Exception:
        return None

    candidates = data.get("candidates", []) if isinstance(data, dict) else []
    if not candidates or not isinstance(candidates[0], dict):
        return None

    cand = candidates[0]
    loc = cand.get("location") or {}
    if not isinstance(loc, dict) or "x" not in loc or "y" not in loc:
        return None

    lon = float(loc["x"])
    lat = float(loc["y"])
    name = cand.get("address") or query

    # ESRI extent → bbox [west, south, east, north], shrunk to zoom in more
    extent = cand.get("extent") or {}
    if extent.get("xmin") is not None:
        w, s, e, n = float(extent["xmin"]), float(extent["ymin"]), float(extent["xmax"]), float(extent["ymax"])
        # Shrink bbox 40% toward center for tighter zoom
        dx, dy = (e - w) * 0.2, (n - s) * 0.2
        bbox = [w + dx, s + dy, e - dx, n - dy]
    else:
        bbox = [lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3]

    return {"name": name, "lat": lat, "lon": lon, "bbox": bbox}


async def _geocode_nominatim(query: str) -> dict[str, Any] | None:
    """Fallback to Nominatim."""
    from urllib.parse import urlencode

    params = urlencode({"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "1"})
    url = f"{NOMINATIM_URL}?{params}"
    req = urlrequest.Request(url, headers={"User-Agent": "EarthAnalyser/1.0"})

    try:
        raw = await asyncio.to_thread(_fetch_url, req)
        results = json.loads(raw)
    except Exception:
        return None

    if not results or not isinstance(results, list):
        return None

    row = results[0]
    lat = float(row["lat"])
    lon = float(row["lon"])
    name = row.get("display_name", query)

    # Nominatim bbox: [south, north, west, east], shrunk to zoom in more
    raw_bbox = row.get("boundingbox")
    if raw_bbox and len(raw_bbox) == 4:
        s, n, w, e = [float(v) for v in raw_bbox]
        # Shrink bbox 40% toward center for tighter zoom
        dx, dy = (e - w) * 0.2, (n - s) * 0.2
        bbox = [w + dx, s + dy, e - dx, n - dy]
    else:
        bbox = [lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3]

    return {"name": name, "lat": lat, "lon": lon, "bbox": bbox}


def _fetch_url(req: urlrequest.Request) -> str:
    with urlrequest.urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8")


# ─── LLM Classification ──────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_ANALYSIS_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

VALID_CATEGORIES = {"fire", "flood", "vegetation", "weather", "landuse"}


class ClassifyRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@router.post("/classify")
async def classify_question(payload: ClassifyRequest):
    """Use LLM to determine which dataset categories are needed to answer a question."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        # Fallback: return all categories
        return {"categories": list(VALID_CATEGORIES)}

    system_prompt = (
        "You are a classifier for a geospatial analysis tool. "
        "Given a user question, return ONLY a JSON array of dataset categories needed to answer it. "
        "Valid categories: fire, flood, vegetation, weather, landuse\n"
        "Rules:\n"
        "- 'fire' = wildfires, burns, hotspots, ignition\n"
        "- 'flood' = water, rivers, rainfall, inundation\n"
        "- 'vegetation' = NDVI, plants, greenery, drought, biomass, fuel load\n"
        "- 'weather' = temperature, heat, cold, LST, climate\n"
        "- 'landuse' = urban, crops, forest, built areas, land cover\n"
        "- For causal/risk questions (why, cause, risk), include supporting categories\n"
        "- For general overview questions, include fire, weather, vegetation\n"
        "Return ONLY a JSON array like [\"fire\",\"weather\"]. No explanation."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload.question},
    ]

    body = json.dumps({
        "model": DEFAULT_ANALYSIS_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 512,
        "provider": {
            "data_collection": "allow",
            "allow_fallbacks": True,
        },
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "EarthAnalyser",
        "HTTP-Referer": "https://earthanalyser.app",
    }

    req = urlrequest.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")

    try:
        raw = await asyncio.to_thread(_fetch_url_post, req)
        result = json.loads(raw)
        msg = result.get("choices", [{}])[0].get("message", {})
        # Check content first, then reasoning (nvidia model puts output in reasoning)
        text = (msg.get("content") or msg.get("reasoning") or "").strip()

        # Extract JSON array from text (may be surrounded by reasoning text)
        import re
        array_match = re.search(r'\[(["\w\s,]+)\]', text)
        if array_match:
            text = array_match.group(0)

        # Handle markdown code blocks
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()

        categories = json.loads(text)
        if isinstance(categories, list):
            valid = [c for c in categories if c in VALID_CATEGORIES]
            if valid:
                return {"categories": valid}
    except Exception:
        pass

    # Fallback: return common categories
    return {"categories": ["fire", "weather", "vegetation"]}


# ─── LLM Analysis ────────────────────────────────────────────


class AnalyseRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    data_report: str = Field(min_length=1, max_length=5000)
    model: str = Field(default=DEFAULT_ANALYSIS_MODEL)


@router.post("/analyse")
async def analyse_with_llm(payload: AnalyseRequest):
    """Send data report + question to an LLM for enhanced reasoning and context."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="LLM_API_KEY not configured")

    system_prompt = (
        "You are Earth Analyst — a friendly, knowledgeable geospatial AI assistant. "
        "You answer questions about Earth observation data the way a helpful colleague would: "
        "clear, conversational, and insightful.\n\n"
        "The user asked a question and the system gathered satellite data for their area. "
        "Below is the raw data report. Your job:\n"
        "1. Answer their question directly and naturally — as if talking to them\n"
        "2. Explain causes, risks, and patterns in plain language\n"
        "3. Add relevant real-world context when it helps understanding\n"
        "4. If interesting, mention what to watch or do next\n\n"
        "Style: conversational, concise (100-200 words), no markdown headers, no bullet lists. "
        "Write in flowing paragraphs like a knowledgeable friend explaining something. "
        "Start directly with your answer — no preamble like 'Based on the data...' or 'Looking at...'. "
        "If there's genuinely interesting or concerning data, say so clearly."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {payload.question}\n\nData Report:\n{payload.data_report}"},
    ]

    body = json.dumps({
        "model": payload.model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1200,
        "provider": {
            "data_collection": "allow",
            "allow_fallbacks": True,
        },
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "EarthAnalyser",
        "HTTP-Referer": "https://earthanalyser.app",
    }

    req = urlrequest.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")

    try:
        raw = await asyncio.to_thread(_fetch_url_post, req)
        result = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}") from exc

    choices = result.get("choices", [])
    if not choices:
        raise HTTPException(status_code=502, detail="LLM returned no response")

    msg = choices[0].get("message", {})
    content = (msg.get("content") or "").strip()
    # Only return the final content, never the raw chain-of-thought reasoning
    if not content:
        raise HTTPException(status_code=502, detail="LLM produced no final response")
    return {"analysis": content, "model": payload.model}


def _fetch_url_post(req: urlrequest.Request) -> str:
    with urlrequest.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")
