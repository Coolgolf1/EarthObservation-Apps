"""
Multi-dataset H3 endpoint — supports fire, flood, vegetation, weather, land use.
Reads rasters from Planetary Computer STAC, converts pixels to H3 hexagons.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
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
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from ..core.config import DATASET_CONFIG

# ─── Memory Safety Constants ─────────────────────────────────
# Machine has ~7.5 GB total RAM (~5 GB usable). Budget conservatively.
MAX_CONCURRENT_RASTERS = 4           # max simultaneous raster reads (global)
MAX_WINDOW_PIXELS = 20_000_000      # cap raster window at ~20M pixels (~80 MB float32)
MAX_H3_INPUT_PIXELS = 10_000_000    # max pixels to convert to H3
MAX_ITEMS_PER_DATASET = 50           # max STAC items processed per dataset request
MAX_CONCURRENT_DATES = 4            # max dates processed in parallel per dataset
MEMORY_HEADROOM_MB = 500            # stop spawning work if available RAM drops below this
_RASTER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RASTERS)

# ─── Active task tracking (for cancellation) ─────────────────
_active_tasks: set[asyncio.Task] = set()


def _check_memory_pressure() -> bool:
    """Return True if system is under memory pressure (should stop loading)."""
    try:
        import psutil
        avail_mb = psutil.virtual_memory().available / (1024 ** 2)
        return avail_mb < MEMORY_HEADROOM_MB
    except ImportError:
        return False

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
    "VSI_CACHE_SIZE": "20000000",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_CONNECTTIMEOUT": "10",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_TCP_KEEPALIVE": "YES",
}

router = APIRouter(prefix="/datasets", tags=["datasets"])

env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(env_path)

CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

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
    if span < 20.0:
        return 4
    if span < 60.0:
        return 3
    if span < 120.0:
        return 2
    return 1

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
    h3_approx_deg = {1: 7.0, 2: 3.0, 3: 1.2, 4: 0.5, 5: 0.2, 6: 0.07, 7: 0.03, 8: 0.01, 9: 0.004}
    target_deg = h3_approx_deg.get(h3_res)
    if target_deg is None:
        return None  # res 10+ → read full resolution
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


def _target_read_shape(window, ds, h3_res: int) -> tuple[int, int] | None:
    """Compute the ideal (height, width) to read, matching pixel size to H3 cell size.
    Returns None if the native resolution is already coarse enough."""
    h3_approx_deg = {1: 7.0, 2: 3.0, 3: 1.2, 4: 0.5, 5: 0.2, 6: 0.07, 7: 0.03, 8: 0.01, 9: 0.004}
    target_deg = h3_approx_deg.get(h3_res)
    if target_deg is None:
        return None
    px_deg = abs(ds.transform.a)
    if px_deg <= 0 or target_deg <= 0:
        return None
    # How many native pixels fit in one H3 cell?  We want ~2 pixels per cell for quality.
    ideal_factor = target_deg / px_deg / 2.0
    if ideal_factor < 1.5:
        return None  # native resolution already close enough
    out_w = max(1, int(window.width / ideal_factor))
    out_h = max(1, int(window.height / ideal_factor))
    if out_w >= window.width and out_h >= window.height:
        return None
    return (out_h, out_w)


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

            # Downsample to match H3 resolution — read only as many pixels as needed
            target_shape = _target_read_shape(window, ds, h3_res)
            if target_shape:
                out_h, out_w = target_shape
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

        # All datasets use STAC COG path
        stac_datasets = valid_datasets

        for dataset_key in stac_datasets:
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
                # Static datasets: query tiles covering the bbox (limited)
                items = await asyncio.to_thread(
                    _query_items_bbox_no_date, catalog, collection, bbox, 5
                )
                unique_items = []
                for item in items:
                    if item.id not in seen_items:
                        seen_items.add(item.id)
                        unique_items.append(item)

                async def _process_static_item(item):
                    asset = _get_asset(item)
                    if not asset or not asset.href:
                        return []
                    async with _RASTER_SEMAPHORE:
                        return await asyncio.to_thread(
                            _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                        )

                results_list = await asyncio.gather(
                    *(_process_static_item(it) for it in unique_items)
                )
                for hexes in results_list:
                    if hexes:
                        dataset_frames.append({"acq_date": "static", "hexagons": hexes})
                        for h in hexes:
                            aggregate_hex[h["h3"]] = h
            else:
                # Single STAC query for the entire date range (instead of per-day)
                all_items = await asyncio.to_thread(
                    _query_items_bbox_daterange, catalog, collection, bbox,
                    payload.date_start, payload.date_end, MAX_ITEMS_PER_DATASET
                )

                # Group items by date for frame organization
                items_by_date: dict[str, list] = {}
                item_count = 0
                for item in all_items:
                    if item.id in seen_items:
                        continue
                    seen_items.add(item.id)
                    acq_date = _item_acq_date(item, payload.date_start)
                    items_by_date.setdefault(acq_date, []).append(item)
                    item_count += 1
                    if item_count >= MAX_ITEMS_PER_DATASET:
                        break

                # Process dates in batches to limit concurrent memory usage
                async def process_item(item, acq_date: str) -> dict[str, Any] | None:
                    if _check_memory_pressure():
                        return None
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

                # Process dates in batches of MAX_CONCURRENT_DATES
                date_keys = list(items_by_date.keys())
                results_list = []
                for batch_start in range(0, len(date_keys), MAX_CONCURRENT_DATES):
                    if _check_memory_pressure():
                        break
                    batch_dates = date_keys[batch_start:batch_start + MAX_CONCURRENT_DATES]
                    batch_tasks = []
                    for acq_date in batch_dates:
                        for item in items_by_date[acq_date]:
                            batch_tasks.append(process_item(item, acq_date))
                    batch_results = await asyncio.gather(*batch_tasks)
                    results_list.extend(batch_results)

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

@router.post("/cancel")
async def cancel_all_requests():
    """Cancel all in-flight dataset processing tasks."""
    cancelled = 0
    for task in list(_active_tasks):
        if not task.done():
            task.cancel()
            cancelled += 1
    _active_tasks.clear()
    return {"cancelled": cancelled}


@router.post("/h3/stream")
async def query_datasets_h3_stream(payload: DatasetH3Request, request: Request):
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
                        _query_items_bbox_no_date, catalog, collection, bbox, 5
                    )
                    unique_items = []
                    for item in items:
                        if item.id not in seen_items:
                            seen_items.add(item.id)
                            unique_items.append(item)

                    async def _proc_static(item):
                        asset = _get_asset(item)
                        if not asset or not asset.href:
                            return []
                        async with _RASTER_SEMAPHORE:
                            return await asyncio.to_thread(
                                _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                            )

                    results_list = await asyncio.gather(
                        *(_proc_static(it) for it in unique_items)
                    )
                    for hexes in results_list:
                        if hexes:
                            acq_date = "static"
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
                        payload.date_start, payload.date_end, MAX_ITEMS_PER_DATASET
                    )

                    # Group items by date for frame organization
                    items_by_date: dict[str, list] = {}
                    item_count = 0
                    for item in all_items:
                        if item.id in seen_items:
                            continue
                        seen_items.add(item.id)
                        acq_date = _item_acq_date(item, payload.date_start)
                        items_by_date.setdefault(acq_date, []).append(item)
                        item_count += 1
                        if item_count >= MAX_ITEMS_PER_DATASET:
                            break

                    # Process dates in batches to control memory
                    async def process_date(acq_date: str, date_items: list):
                        async def process_item(item) -> list[dict[str, Any]]:
                            if _check_memory_pressure():
                                return []
                            asset = _get_asset(item)
                            if not asset or not asset.href:
                                return []
                            async with _RASTER_SEMAPHORE:
                                return await asyncio.to_thread(
                                    _raster_to_h3, asset.href, bbox, ds_h3_res, dataset_key
                                )

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
                            return merged_hexes
                        return []

                    # Process dates in batches of MAX_CONCURRENT_DATES
                    date_keys = list(items_by_date.keys())
                    date_results = []
                    for batch_start in range(0, len(date_keys), MAX_CONCURRENT_DATES):
                        if _check_memory_pressure():
                            break
                        batch_dates = date_keys[batch_start:batch_start + MAX_CONCURRENT_DATES]
                        batch_results = await asyncio.gather(
                            *(process_date(ad, items_by_date[ad]) for ad in batch_dates)
                        )
                        date_results.extend(batch_results)
                    for merged_hexes in date_results:
                        if merged_hexes:
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
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)

            # Yield results as they arrive, checking for client disconnect
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    task.cancel()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                yield item

            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

            # Send done signal
            yield json.dumps({"type": "done"}) + "\n"

        except asyncio.CancelledError:
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


async def _llm_fix_place_name(query: str) -> str:
    """Use lightweight Gemma to correct typos in a place name before geocoding."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key or len(query) <= 2:
        return query

    messages = [
        {
            "role": "system",
            "content": (
                "You fix place names. Given a potentially misspelled place name or phrase, "
                "return ONLY the corrected place name. No explanation, no quotes, no punctuation — "
                "just the clean place name.\n"
                "Examples:\n"
                "- 'Spaen' → 'Spain'\n"
                "- 'Lsibon' → 'Lisbon'\n"
                "- 'the north of portugl' → 'northern Portugal'\n"
                "- 'calfornia' → 'California'\n"
                "- 'madird' → 'Madrid'\n"
                "- 'southern frace' → 'southern France'\n"
                "If the input is already correct, return it unchanged."
            ),
        },
        {"role": "user", "content": query},
    ]

    body = json.dumps({
        "model": TYPO_FIX_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 60,
        "provider": {"data_collection": "allow", "allow_fallbacks": True},
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
        text = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .strip("\"'`")
        )
        if text and len(text) <= 100 and "\n" not in text:
            return text
    except Exception:
        pass

    return query


async def _llm_fix_query(question: str) -> str:
    """Use lightweight Gemma to read the user's entire phrase and fix all spelling errors."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key or len(question) <= 2:
        return question

    messages = [
        {
            "role": "system",
            "content": (
                "You are a spell-checker. Read the user's entire sentence and fix ALL "
                "spelling errors — place names, category words, dates, everything. "
                "Return ONLY the corrected sentence. Keep the same meaning and structure. "
                "No explanation, no extra text.\n"
                "Examples:\n"
                "- 'frie in spaen last sumer' → 'fire in Spain last summer'\n"
                "- 'flod in portugl 2023' → 'flood in Portugal 2023'\n"
                "- 'vegitation helth in calfornia' → 'vegetation health in California'\n"
                "- 'wether and temprature in madird' → 'weather and temperature in Madrid'\n"
                "- 'show fiers near lsibon' → 'show fires near Lisbon'\n"
                "- 'landuse in souther frace' → 'land use in southern France'\n"
                "If the input is already correct, return it unchanged."
            ),
        },
        {"role": "user", "content": question},
    ]

    body = json.dumps({
        "model": TYPO_FIX_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 120,
        "provider": {"data_collection": "allow", "allow_fallbacks": True},
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
        text = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .strip("\"'`")
        )
        if text and len(text) <= 500 and "\n" not in text:
            return text
    except Exception:
        pass

    return question


@router.post("/geocode")
async def geocode_place(payload: GeocodeRequest):
    """Resolve a place name to coordinates and bbox using ESRI (primary) with Nominatim fallback.
    For countries/regions, also returns a GeoJSON polygon for clipping."""
    query = payload.query.strip()

    # --- LLM pre-processing: fix typos/misspellings before geocoding ---
    query = await _llm_fix_place_name(query)

    cache_key = query.lower()

    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # --- Try Nominatim first (returns polygon for countries/regions) ---
    result = await _geocode_nominatim(query)

    # --- Fallback to ESRI ---
    if result is None:
        result = await _geocode_esri(query)

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

    # ESRI extent → bbox [west, south, east, north]
    extent = cand.get("extent") or {}
    if extent.get("xmin") is not None:
        w, s, e, n = float(extent["xmin"]), float(extent["ymin"]), float(extent["xmax"]), float(extent["ymax"])
        bbox = [w, s, e, n]
    else:
        bbox = [lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3]

    return {"name": name, "lat": lat, "lon": lon, "bbox": bbox}


async def _geocode_nominatim(query: str) -> dict[str, Any] | None:
    """Fallback to Nominatim. Returns polygon for countries/regions."""
    from urllib.parse import urlencode

    params = urlencode({
        "q": query, "format": "jsonv2", "limit": "1",
        "addressdetails": "1", "polygon_geojson": "1",
    })
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
    category = row.get("category", "")
    feat_type = row.get("type", "")
    osm_type = row.get("osm_type", "")

    # Nominatim bbox: [south, north, west, east]
    raw_bbox = row.get("boundingbox")
    if raw_bbox and len(raw_bbox) == 4:
        s, n, w, e = [float(v) for v in raw_bbox]
        bbox = [w, s, e, n]
        lon_span = e - w
        lat_span = n - s

        # Detect point-like results for geographic features (node with tiny bbox)
        is_point_like = lon_span < 0.01 and lat_span < 0.01
        is_geo_feature = category in ("natural", "water", "waterway", "geological") or feat_type in (
            "desert", "lake", "river", "mountain", "mountain_range", "volcano",
            "glacier", "valley", "canyon", "plateau", "island", "peninsula",
            "bay", "gulf", "sea", "ocean", "reef", "wetland", "marsh", "basin",
            "cape", "strait", "fjord", "crater", "national_park", "nature_reserve",
        )

        if is_point_like and is_geo_feature:
            # Expand based on feature type — larger features get bigger bbox
            _FEATURE_EXPANSION = {
                "desert": 8.0, "ocean": 10.0, "sea": 6.0,
                "mountain_range": 4.0, "plateau": 4.0, "basin": 4.0,
                "lake": 2.0, "river": 3.0, "glacier": 2.0,
                "island": 2.0, "peninsula": 3.0, "gulf": 4.0, "bay": 2.0,
                "valley": 2.0, "canyon": 1.5, "volcano": 0.5, "mountain": 0.5,
                "wetland": 2.0, "marsh": 1.5, "reef": 2.0,
                "cape": 1.0, "strait": 2.0, "fjord": 1.0, "crater": 0.5,
                "national_park": 1.5, "nature_reserve": 1.5,
            }
            expansion = _FEATURE_EXPANSION.get(feat_type, 3.0)
            bbox = [lon - expansion, lat - expansion * 0.7, lon + expansion, lat + expansion * 0.7]
        elif lon_span > 12.0 or lat_span > 12.0:
            # If bbox is excessively wide due to remote territories/islands,
            # shrink it to a reasonable area around the centroid
            max_reasonable_span = 12.0
            half_lon = min(lon_span, max_reasonable_span) / 2
            half_lat = min(lat_span, max_reasonable_span) / 2
            bbox = [lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat]
    else:
        bbox = [lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3]

    # Include polygon for countries/regions (type: administrative, etc.)
    geojson = row.get("geojson")
    result: dict[str, Any] = {"name": name, "lat": lat, "lon": lon, "bbox": bbox}
    if geojson and geojson.get("type") in ("Polygon", "MultiPolygon"):
        result["polygon"] = geojson

    return result


def _fetch_url(req: urlrequest.Request) -> str:
    with urlrequest.urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8")


# ─── LLM Classification ──────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_ANALYSIS_MODEL = "qwen/qwen3-235b-a22b:free"
TYPO_FIX_MODEL = os.getenv("LLM_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

VALID_CATEGORIES = {"fire", "flood", "vegetation", "weather", "landuse"}

# Simple keyword fallback — only used when LLM is unavailable.
# Includes common typos/misspellings so regex catches them even without LLM.
_CATEGORY_KEYWORDS = {
    "fire":       re.compile(r"fire|frie|fier|fyre|fir[ea]s|burn|wildfire|incendio|fuego", re.I),
    "flood":      re.compile(r"flood|flod|flud|water|rain|river|inundaci", re.I),
    "vegetation": re.compile(r"veget|vegitat|ndvi|plant|green|drought|plantas", re.I),
    "weather":    re.compile(r"weather|wether|wheather|temp|heat|hot|cold|climate|calor", re.I),
    "landuse":    re.compile(r"land.?use|urban|crop|forest|city|ciudad", re.I),
}


def _infer_categories(question: str) -> list[str]:
    """Keyword fallback with typo support — used when LLM is unavailable."""
    q = question.lower()
    cats = [cat for cat, pat in _CATEGORY_KEYWORDS.items() if pat.search(q)]
    if not cats and re.search(r"what.*(happen|going|see)|analyz|summar|overview|everything|all", q):
        cats = ["fire", "flood", "weather", "vegetation", "landuse"]
    return cats or ["fire"]  # if nothing matches, default to fire


class ClassifyRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@router.post("/classify")
async def classify_question(payload: ClassifyRequest):
    """Use LLM to determine which dataset categories and date range are needed."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return {"categories": _infer_categories(payload.question)}

    # Step 1: Fix typos with fast Gemma before classifying
    question = await _llm_fix_query(payload.question)

    system_prompt = (
        "You are a classifier for a geospatial analysis tool. "
        "Given a user question in natural language (which may contain typos, slang, or casual phrasing), "
        "infer what they mean and return ONLY a JSON object with:\n"
        "  \"categories\": array of dataset categories needed\n"
        "  \"context_categories\": array of supporting datasets for analysis (loaded but not displayed)\n"
        "  \"date_start\": start date (YYYY-MM-DD) if mentioned or implied, or null\n"
        "  \"date_end\": end date (YYYY-MM-DD) if mentioned or implied, or null\n"
        "Valid categories: fire, flood, vegetation, weather, landuse\n"
        "Rules:\n"
        "- 'fire' = wildfires, burns, hotspots, ignition (also: fier, fyre, incendio, fuego)\n"
        "- 'flood' = water, rivers, rainfall, inundation, tsunami (also: flod, inundacion)\n"
        "- 'vegetation' = NDVI, plants, greenery, drought, biomass (also: vegitation, plantas)\n"
        "- 'weather' = temperature, heat, cold, LST, climate (also: wether, wheather, temperatura)\n"
        "- 'landuse' = urban, crops, forest, built areas, land cover\n"
        "- The user may misspell words — infer the correct meaning from context\n"
        "- For causal/why questions, add supporting categories to context_categories\n"
        "- For general overview questions, include all relevant categories\n"
        f"- TODAY is {date.today().isoformat()}. Use this to resolve relative dates:\n"
        "  - 'last month' = first to last day of the previous calendar month\n"
        "  - 'this month' = first day of current month to today\n"
        "  - 'recently' = last 30 days\n"
        "  - 'last summer' = June 1 to Aug 31 of the previous year\n"
        "  - 'summer 2024' = 2024-06-01 to 2024-08-31\n"
        "  - 'last year' = Jan 1 to Dec 31 of the previous year\n"
        "  - 'this year' = Jan 1 of current year to today\n"
        "  - 'in 2023' = 2023-01-01 to 2023-12-31\n"
        "  - 'yesterday' = the day before today\n"
        "  - 'last week' = 7 days before today to today\n"
        "  - 'in June' = June 1 to June 30 of the current or most recent June\n"
        "  - 'since March' = March 1 of the most recent March to today\n"
        "- If no date is mentioned at all, set both to null\n"
        "Return ONLY the JSON object. No explanation.\n"
        "Example: {\"categories\":[\"fire\",\"weather\"],\"context_categories\":[\"vegetation\"],\"date_start\":\"2024-06-01\",\"date_end\":\"2024-08-31\"}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
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
        text = (msg.get("content") or msg.get("reasoning") or "").strip()

        # Handle markdown code blocks
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()

        # Try to extract a JSON object first (new format)
        obj_match = re.search(r'\{[^{}]+\}', text)
        if obj_match:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                cats = parsed.get("categories", [])
                valid = [c for c in cats if c in VALID_CATEGORIES]
                ctx_cats = parsed.get("context_categories", [])
                valid_ctx = [c for c in ctx_cats if c in VALID_CATEGORIES]
                resp = {
                    "categories": valid or _infer_categories(question),
                    "context_categories": valid_ctx,
                }
                ds = parsed.get("date_start")
                de = parsed.get("date_end")
                if ds and re.match(r'^\d{4}-\d{2}-\d{2}$', str(ds)):
                    resp["date_start"] = str(ds)
                if de and re.match(r'^\d{4}-\d{2}-\d{2}$', str(de)):
                    resp["date_end"] = str(de)
                return resp

        # Fallback: try JSON array (old format)
        array_match = re.search(r'\[(["\w\s,]+)\]', text)
        if array_match:
            categories = json.loads(array_match.group(0))
            if isinstance(categories, list):
                valid = [c for c in categories if c in VALID_CATEGORIES]
                if valid:
                    return {"categories": valid}
    except Exception:
        pass

    # Fallback: keyword-based inference on corrected question
    return {"categories": _infer_categories(question)}


# ─── LLM Analysis ────────────────────────────────────────────


class AnalyseRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    data_report: str = Field(default="", max_length=5000)
    model: str = Field(default=DEFAULT_ANALYSIS_MODEL)


@router.post("/analyse")
async def analyse_with_llm(payload: AnalyseRequest):
    """Send data report + question to an LLM for enhanced reasoning and context."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="LLM_API_KEY not configured")

    system_prompt = (
        "You are Earth Analyst — a friendly, knowledgeable geospatial AI assistant. "
        "You have a natural, conversational style. You talk like a smart colleague "
        "who genuinely enjoys explaining things about our planet.\n\n"
        "The user is chatting with you while looking at a map with satellite data overlaid. "
        "Below is the raw data the system gathered for their area and time range.\n\n"
        "Your job:\n"
        "1. Answer their question directly — don't repeat what they asked\n"
        "2. Be specific: use actual numbers from the data (temperatures, percentages, counts)\n"
        "3. Explain WHY things happen, not just WHAT happened\n"
        "4. Connect different datasets when relevant (e.g., high temps + low NDVI = fire risk)\n"
        "5. Add real-world context when helpful (regional climate, land use patterns, seasonal norms)\n"
        "6. If the data reveals something interesting or concerning, highlight it\n"
        "7. If data is missing or insufficient, say so honestly rather than guessing\n\n"
        "Style rules:\n"
        "- Be concise: 80-200 words unless the question needs more\n"
        "- No markdown headers or bullet lists — write in flowing, natural paragraphs\n"
        "- Start directly with your answer. Never start with 'Based on the data...',  "
        "'Looking at...', or 'According to...'\n"
        "- Use plain language: '35°C is scorching' not 'the temperature anomaly is significant'\n"
        "- You can use light formatting: bold a key number, use an emoji if it fits naturally\n"
        "- If the user's question is casual ('what's up with the fires?'), match that tone\n"
        "- If they ask a technical question, be precise\n"
        "- You can suggest what to look at next if relevant"
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
        # Fallback: return a basic analysis from the data report
        return {
            "analysis": _fallback_analysis(payload.question, payload.data_report),
            "model": "fallback",
        }

    choices = result.get("choices", [])
    if not choices:
        return {
            "analysis": _fallback_analysis(payload.question, payload.data_report),
            "model": "fallback",
        }

    msg = choices[0].get("message", {})
    content = (msg.get("content") or "").strip()
    # Only return the final content, never the raw chain-of-thought reasoning
    if not content:
        return {
            "analysis": _fallback_analysis(payload.question, payload.data_report),
            "model": "fallback",
        }
    return {"analysis": content, "model": payload.model}


def _fallback_analysis(question: str, data_report: str) -> str:
    """Generate a simple analysis when LLM is unavailable."""
    return data_report


def _fetch_url_post(req: urlrequest.Request) -> str:
    with urlrequest.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")
