"""
Expanded endpoint + rendering test for Planetary Computer collections.

Tests:
1. Data retrieval (STAC search + raster download) for 15 days
2. H3 hexagon conversion time (pixel → H3 aggregation)
3. Raster-based rendering time (direct COG → image)
4. Total pipeline time (load + render)

Thresholds:
- Data loading: < 30s total for 15 days
- Rendering (H3 or raster): < 5s target, max 10s
- Single request: < 10s, max 20s

Usage:
    python test_rendering.py
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import h3
import httpx
import numpy as np
import planetary_computer
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

# ─── Configuration ────────────────────────────────────────────────────────────
STAC_API = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Test bbox: small area in Portugal (~0.5° × 0.5°)
TEST_BBOX = [-8.5, 39.5, -8.0, 40.0]

# Time window
END_DATE = date(2025, 6, 1)
START_DATE = END_DATE - timedelta(days=15)

# Timeouts
SINGLE_REQUEST_TIMEOUT = 20.0
TOTAL_TIMEOUT = 30.0
RENDER_TARGET = 5.0   # target render time
RENDER_MAX = 10.0     # absolute max render time

# GDAL optimizations for COG reads
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
}

# ─── Dataset configurations ───────────────────────────────────────────────────
DATASETS = {
    "fire": {
        "collection": "modis-14A1-061",
        "asset": "FireMask",
        "description": "MODIS Daily Fire 1km",
        "frequency": "Daily",
        "value_type": "classification",
        "filter": lambda arr: (arr >= 7) & (arr <= 9),
        "classify": lambda val: "high" if val >= 9 else "nominal" if val >= 8 else "low",
        "colormap": {7: (255, 255, 0), 8: (255, 165, 0), 9: (255, 0, 0)},
        "h3_res": 7,
    },
    "fire_s3": {
        "collection": "sentinel-3-slstr-frp-l2-netcdf",
        "asset": "frp-in",
        "description": "Sentinel-3 Fire Radiative Power",
        "frequency": "Daily",
        "value_type": "netcdf",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "active",
        "colormap": None,
        "h3_res": 7,
    },
    "flood_sar": {
        "collection": "sentinel-1-rtc",
        "asset": "vv",
        "description": "Sentinel-1 RTC SAR (flood detection)",
        "frequency": "6-12 days",
        "value_type": "continuous",
        "filter": lambda arr: arr < -15,  # low backscatter = water
        "classify": lambda val: "water" if val < -20 else "possible_water",
        "colormap": None,
        "h3_res": 7,
    },
    "flood_jrc": {
        "collection": "jrc-gsw",
        "asset": "occurrence",
        "description": "JRC Global Surface Water",
        "frequency": "Static",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "permanent" if val >= 80 else "seasonal" if val >= 30 else "ephemeral",
        "colormap": None,
        "h3_res": 7,
    },
    "vegetation_ndvi": {
        "collection": "modis-13A1-061",
        "asset": "500m_16_days_NDVI",
        "description": "MODIS NDVI 16-day 500m",
        "frequency": "16-day",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "dense" if val > 6000 else "moderate" if val > 3000 else "sparse",
        "colormap": None,
        "h3_res": 7,
    },
    "vegetation_lai": {
        "collection": "modis-15A2H-061",
        "asset": "Lai_500m",
        "description": "MODIS LAI 8-day 500m",
        "frequency": "8-day",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "high" if val > 50 else "medium" if val > 20 else "low",
        "colormap": None,
        "h3_res": 7,
    },
    "vegetation_s2": {
        "collection": "sentinel-2-l2a",
        "asset": "B08",
        "description": "Sentinel-2 L2A 10m (NIR for NDVI)",
        "frequency": "5-day",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "vegetation" if val > 3000 else "other",
        "colormap": None,
        "h3_res": 7,  # keep at 7 even for 10m (avoid millions of hexagons)
    },
    "vegetation_nbar": {
        "collection": "modis-43A4-061",
        "asset": "Nadir_Reflectance_Band1",
        "description": "MODIS NBAR Daily 500m",
        "frequency": "Daily",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "bright" if val > 3000 else "medium" if val > 1500 else "dark",
        "colormap": None,
        "h3_res": 7,
    },
    "landuse_esa": {
        "collection": "esa-worldcover",
        "asset": "map",
        "description": "ESA WorldCover 10m",
        "frequency": "Annual",
        "value_type": "classification",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "tree" if val == 10 else "shrub" if val == 20 else "grass" if val == 30
            else "crop" if val == 40 else "built" if val == 50 else "bare" if val == 60
            else "water" if val == 80 else "other"
        ),
        "colormap": {10: (0, 100, 0), 20: (150, 100, 0), 30: (255, 255, 100),
                     40: (255, 200, 100), 50: (200, 0, 0), 60: (200, 200, 200),
                     80: (0, 0, 200)},
        "h3_res": 7,  # cap at 7 for 10m data to keep H3 fast
    },
    "landuse_esri": {
        "collection": "io-lulc-annual-v02",
        "asset": "data",
        "description": "Esri 10m LULC Annual",
        "frequency": "Annual",
        "value_type": "classification",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: str(val),
        "colormap": None,
        "h3_res": 7,
    },
    "weather_lst_daily": {
        "collection": "modis-11A1-061",
        "asset": "QC_Day",
        "description": "MODIS Daily LST 1km",
        "frequency": "Daily",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "good" if val < 65 else "moderate" if val < 129 else "poor",
        "colormap": None,
        "h3_res": 7,
    },
    "weather_lst_8day": {
        "collection": "modis-11A2-061",
        "asset": "LST_Day_1km",
        "description": "MODIS LST 8-day 1km",
        "frequency": "8-day",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "hot" if val > 15000 else "warm" if val > 14000 else "cool",
        "colormap": None,
        "h3_res": 7,
    },
    "weather_met_office": {
        "collection": "met-office-global-deterministic-pressure",
        "asset": "temperature_on_pressure_levels",
        "description": "Met Office Global 10km Forecast",
        "frequency": "6-hourly",
        "value_type": "netcdf",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "data",
        "colormap": None,
        "h3_res": 5,
    },
    "weather_snow": {
        "collection": "modis-10A1-061",
        "asset": "NDSI_Snow_Cover",
        "description": "MODIS Snow Cover Daily 500m",
        "frequency": "Daily",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: "snow" if val > 50 else "partial" if val > 10 else "none",
        "colormap": None,
        "h3_res": 7,
    },
    "dem_copernicus": {
        "collection": "cop-dem-glo-30",
        "asset": "data",
        "description": "Copernicus DEM 30m",
        "frequency": "Static",
        "value_type": "continuous",
        "filter": lambda arr: arr > -500,  # valid elevation
        "classify": lambda val: "high" if val > 1000 else "medium" if val > 200 else "low",
        "colormap": None,
        "h3_res": 7,
    },
}


@dataclass
class RenderTestResult:
    dataset: str
    description: str
    frequency: str
    # Data loading
    items_found: int = 0
    load_time_s: float = 0.0
    raster_shape: tuple = ()
    pixels_loaded: int = 0
    # H3 conversion
    h3_resolution: int = 7
    h3_convert_time_s: float = 0.0
    h3_hexagons: int = 0
    h3_geojson_size_kb: float = 0.0
    # Raster rendering
    raster_render_time_s: float = 0.0
    raster_image_size_kb: float = 0.0
    # Total
    total_pipeline_time_s: float = 0.0
    # Verdict
    h3_passes: bool = False
    raster_passes: bool = False
    recommended_method: str = ""
    error: str = ""
    notes: str = ""


# ─── STAC Search ──────────────────────────────────────────────────────────────

async def stac_search(
    client: httpx.AsyncClient,
    collection: str,
    bbox: list[float],
    start: date,
    end: date,
    limit: int = 15,
) -> list[dict]:
    """Search STAC API with date range fallback."""
    for dt_range in [
        (start, end),
        (end - timedelta(days=90), end),
        (end - timedelta(days=365), end),
        (end - timedelta(days=365 * 5), end),
    ]:
        payload = {
            "collections": [collection],
            "bbox": bbox,
            "datetime": f"{dt_range[0].isoformat()}T00:00:00Z/{dt_range[1].isoformat()}T23:59:59Z",
            "limit": limit,
        }
        resp = await client.post(f"{STAC_API}/search", json=payload, timeout=SINGLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("features", [])
        if items:
            return items

    # No date filter (static datasets)
    payload = {"collections": [collection], "bbox": bbox, "limit": limit}
    resp = await client.post(f"{STAC_API}/search", json=payload, timeout=SINGLE_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("features", [])


def _get_signed_href(item: dict, asset_key: str) -> str | None:
    """Get signed URL for an asset."""
    assets = item.get("assets", {})
    if asset_key not in assets:
        # Fallback: first GeoTIFF asset
        for k, v in assets.items():
            if "tiff" in v.get("type", "").lower() or "geotiff" in v.get("type", "").lower():
                asset_key = k
                break
        else:
            return None
    href = assets[asset_key]["href"]
    return planetary_computer.sign(href)


# ─── Raster Loading ──────────────────────────────────────────────────────────

def load_raster_for_bbox(href: str, bbox: list[float], max_pixels: int = 5_000_000) -> tuple[np.ndarray, rasterio.Affine, str] | None:
    """Load raster data for a bbox, downsampling if needed. Returns (data, transform, crs)."""
    west, south, east, north = bbox
    try:
        with rasterio.Env(**_GDAL_ENV):
            with rasterio.open(href) as ds:
                # Transform bbox to dataset CRS
                minx, miny, maxx, maxy = transform_bounds(
                    "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
                )
                window = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
                window = window.intersection(Window(0, 0, ds.width, ds.height))
                if window.width <= 0 or window.height <= 0:
                    return None
                window = window.round_offsets().round_lengths()

                # Downsample if too many pixels
                total_px = int(window.width * window.height)
                if total_px > max_pixels:
                    scale = math.sqrt(max_pixels / total_px)
                    out_h = max(1, int(window.height * scale))
                    out_w = max(1, int(window.width * scale))
                else:
                    out_h, out_w = int(window.height), int(window.width)

                data = ds.read(
                    1, window=window, out_shape=(out_h, out_w),
                    masked=False, resampling=rasterio.enums.Resampling.nearest,
                ).astype(np.float32)

                win_transform = rasterio.windows.transform(window, ds.transform)
                # Adjust transform if downsampled
                if out_w != int(window.width) or out_h != int(window.height):
                    win_transform = rasterio.Affine(
                        win_transform.a * (window.width / out_w),
                        win_transform.b, win_transform.c,
                        win_transform.d,
                        win_transform.e * (window.height / out_h),
                        win_transform.f,
                    )
                return data, win_transform, str(ds.crs)
    except Exception:
        return None


# ─── H3 Conversion ───────────────────────────────────────────────────────────

def raster_to_h3(
    data: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
    h3_res: int,
    filter_fn,
    classify_fn,
) -> list[dict]:
    """
    Convert raster pixels to H3 hexagons with aggregated values.
    Returns list of hexagon dicts with boundary polygons for plotting.
    """
    # Apply filter
    mask = filter_fn(data)
    if not np.any(mask):
        return []

    # Get coordinates of valid pixels
    rows, cols = np.where(mask)
    values = data[rows, cols]

    # Subsample if too many pixels (>500K for H3 conversion speed)
    if len(rows) > 500_000:
        step = len(rows) // 500_000
        rows = rows[::step]
        cols = cols[::step]
        values = values[::step]

    # Convert pixel coords to lat/lon
    xs = transform.c + cols * transform.a + rows * transform.b
    ys = transform.f + cols * transform.d + rows * transform.e

    # If not WGS84, we'd need to reproject (skip for MODIS which is typically sinusoidal)
    # For this test, approximate: treat as WGS84 (sufficient for benchmarking)
    if "4326" not in crs and "sinusoidal" in crs.lower():
        # Rough sinusoidal→WGS84: lon = x/cos(lat_rad), lat = y
        # (for benchmarking, exact proj isn't critical)
        lats = ys / 111320.0 if abs(ys.mean()) > 180 else ys
        lons = xs / (111320.0 * np.cos(np.radians(lats.mean()))) if abs(xs.mean()) > 180 else xs
    else:
        lats = ys
        lons = xs

    # Clip to valid range
    lats = np.clip(lats, -90, 90)
    lons = np.clip(lons, -180, 180)

    # Convert to H3
    hex_data = defaultdict(lambda: {"values": [], "count": 0, "classes": defaultdict(int)})

    for i in range(len(lats)):
        try:
            h3_idx = h3.latlng_to_cell(float(lats[i]), float(lons[i]), h3_res)
        except Exception:
            continue
        hex_data[h3_idx]["values"].append(float(values[i]))
        hex_data[h3_idx]["count"] += 1
        cls = classify_fn(float(values[i]))
        hex_data[h3_idx]["classes"][cls] += 1

    # Build hexagon output with boundaries (for precise polygon plotting)
    hexagons = []
    for h3_idx, hd in hex_data.items():
        boundary = h3.cell_to_boundary(h3_idx)  # list of (lat, lng) tuples
        center = h3.cell_to_latlng(h3_idx)
        vals = hd["values"]
        dominant_class = max(hd["classes"], key=hd["classes"].get) if hd["classes"] else ""
        hexagons.append({
            "h3": h3_idx,
            "boundary": [[lat, lng] for lat, lng in boundary],
            "center": list(center),
            "pixel_count": hd["count"],
            "value_mean": sum(vals) / len(vals) if vals else 0,
            "value_max": max(vals) if vals else 0,
            "value_min": min(vals) if vals else 0,
            "dominant_class": dominant_class,
            "classes": dict(hd["classes"]),
        })

    return hexagons


def h3_to_geojson(hexagons: list[dict]) -> dict:
    """Convert H3 hexagons to GeoJSON FeatureCollection for map rendering."""
    features = []
    for h in hexagons:
        # H3 boundary is (lat, lng) but GeoJSON needs [lng, lat]
        coords = [[lng, lat] for lat, lng in h["boundary"]]
        coords.append(coords[0])  # close the ring
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords],
            },
            "properties": {
                "h3": h["h3"],
                "pixel_count": h["pixel_count"],
                "value_mean": round(h["value_mean"], 2),
                "value_max": round(h["value_max"], 2),
                "dominant_class": h["dominant_class"],
                "classes": h["classes"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ─── Raster-Based Rendering ──────────────────────────────────────────────────

def raster_to_png_bytes(
    data: np.ndarray,
    transform: rasterio.Affine,
    bbox: list[float],
    colormap: dict | None = None,
    width: int = 512,
    height: int = 512,
) -> bytes:
    """
    Render raster data to a PNG image for direct map overlay.
    Uses colormap for classified data, or viridis-style gradient for continuous.
    Returns raw PNG bytes (no matplotlib dependency).
    """
    # Normalize data to 0-255 for PNG
    h, w = data.shape
    if h == 0 or w == 0:
        return b""

    if colormap:
        # Classification: map values to RGB
        img = np.zeros((h, w, 4), dtype=np.uint8)
        for val, (r, g, b) in colormap.items():
            mask = (data >= val - 0.5) & (data < val + 0.5)
            img[mask] = [r, g, b, 200]  # semi-transparent
    else:
        # Continuous: normalize to gradient (blue → green → yellow → red)
        valid = data[data > 0] if np.any(data > 0) else data.ravel()
        if valid.size == 0:
            return b""
        vmin, vmax = np.nanpercentile(valid, [2, 98])
        if vmax <= vmin:
            vmax = vmin + 1

        normalized = np.clip((data - vmin) / (vmax - vmin), 0, 1)

        # Simple viridis-like colormap (4 stops)
        img = np.zeros((h, w, 4), dtype=np.uint8)
        mask = data > 0

        r = np.clip((normalized * 3 - 2) * 255, 0, 255).astype(np.uint8)
        g = np.clip(np.where(normalized < 0.5, normalized * 2, (1 - normalized) * 2) * 255, 0, 255).astype(np.uint8)
        b = np.clip((1 - normalized) * 255, 0, 255).astype(np.uint8)

        img[mask, 0] = r[mask]
        img[mask, 1] = g[mask]
        img[mask, 2] = b[mask]
        img[mask, 3] = 200  # semi-transparent

    # Resize to target dimensions if needed
    if h > 0 and w > 0 and (h != height or w != width):
        # Simple nearest-neighbor resize
        row_idx = np.clip((np.arange(height) * h // height).astype(int), 0, h - 1)
        col_idx = np.clip((np.arange(width) * w // width).astype(int), 0, w - 1)
        img = img[row_idx][:, col_idx]
    elif h == 0 or w == 0:
        return b""

    # Encode as minimal PNG (no external dependency)
    return _encode_png(img)


def _encode_png(img: np.ndarray) -> bytes:
    """Minimal PNG encoder for RGBA images (no PIL/matplotlib needed)."""
    import zlib

    h, w, _ = img.shape

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + chunk + crc

    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    ihdr = make_chunk(b"IHDR", ihdr_data)

    # IDAT (filtered rows)
    raw = b""
    for row in range(h):
        raw += b"\x00" + img[row].tobytes()  # filter type 0 (None) for each row
    compressed = zlib.compress(raw, level=6)
    idat = make_chunk(b"IDAT", compressed)

    # IEND
    iend = make_chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


def raster_to_tile_response(
    data: np.ndarray,
    transform: rasterio.Affine,
    bbox: list[float],
    colormap: dict | None = None,
) -> dict:
    """
    Create a map tile response (for Leaflet/MapLibre ImageOverlay).
    Returns bounds + base64 PNG for immediate frontend rendering.
    """
    import base64

    t0 = time.time()
    png_bytes = raster_to_png_bytes(data, transform, bbox, colormap)
    render_time = time.time() - t0

    west, south, east, north = bbox
    return {
        "type": "raster_overlay",
        "bounds": [[south, west], [north, east]],
        "image_base64": base64.b64encode(png_bytes).decode() if png_bytes else "",
        "render_time_s": render_time,
        "image_size_kb": len(png_bytes) / 1024,
        "dimensions": data.shape,
    }


# ─── Test Runner ──────────────────────────────────────────────────────────────

async def test_dataset(client: httpx.AsyncClient, key: str) -> RenderTestResult:
    """Full pipeline test for a dataset: load → H3 → raster render."""
    config = DATASETS[key]
    result = RenderTestResult(
        dataset=key,
        description=config["description"],
        frequency=config["frequency"],
        h3_resolution=config["h3_res"],
    )

    # Skip NetCDF assets (not COG-readable)
    if config["value_type"] == "netcdf":
        result.notes = "NetCDF format — STAC search OK but COG raster test skipped"
        try:
            t0 = time.time()
            items = await stac_search(client, config["collection"], TEST_BBOX, START_DATE, END_DATE)
            result.load_time_s = time.time() - t0
            result.items_found = len(items)
            result.notes += f" | {len(items)} items available"
        except Exception as e:
            result.error = str(e)
        return result

    try:
        # ─── Phase 1: Load data ───────────────────────────────────────────
        t_start = time.time()

        items = await stac_search(client, config["collection"], TEST_BBOX, START_DATE, END_DATE)
        result.items_found = len(items)

        if not items:
            result.error = "No items found"
            return result

        # Get signed href for first item
        href = _get_signed_href(items[0], config["asset"])
        if not href:
            result.error = "No readable asset found"
            return result

        # Load raster
        raster_result = await asyncio.to_thread(load_raster_for_bbox, href, TEST_BBOX)
        result.load_time_s = time.time() - t_start

        if raster_result is None:
            result.error = "Failed to load raster data (empty window)"
            return result

        data, transform, crs = raster_result
        result.raster_shape = data.shape
        result.pixels_loaded = int(data.size)

        # ─── Phase 2: H3 Conversion ──────────────────────────────────────
        t_h3 = time.time()
        hexagons = await asyncio.to_thread(
            raster_to_h3, data, transform, crs,
            config["h3_res"], config["filter"], config["classify"],
        )
        result.h3_convert_time_s = time.time() - t_h3
        result.h3_hexagons = len(hexagons)

        # GeoJSON size (for frontend payload estimation)
        if hexagons:
            geojson = h3_to_geojson(hexagons)
            import json
            geojson_str = json.dumps(geojson)
            result.h3_geojson_size_kb = len(geojson_str) / 1024

        # ─── Phase 3: Raster Rendering ────────────────────────────────────
        t_raster = time.time()
        tile_result = await asyncio.to_thread(
            raster_to_tile_response, data, transform, TEST_BBOX, config.get("colormap"),
        )
        result.raster_render_time_s = time.time() - t_raster
        result.raster_image_size_kb = tile_result["image_size_kb"]

        # ─── Totals & Verdicts ────────────────────────────────────────────
        result.total_pipeline_time_s = time.time() - t_start
        result.h3_passes = result.h3_convert_time_s < RENDER_MAX
        result.raster_passes = result.raster_render_time_s < RENDER_MAX

        # Determine recommendation
        if result.raster_render_time_s < result.h3_convert_time_s * 0.5:
            result.recommended_method = "RASTER"
            result.notes = "Raster significantly faster; use ImageOverlay for map rendering"
        elif result.h3_convert_time_s < RENDER_TARGET and result.h3_hexagons < 5000:
            result.recommended_method = "H3"
            result.notes = "H3 fast with reasonable hexagon count; good for interactive filtering"
        elif result.raster_render_time_s < RENDER_TARGET:
            result.recommended_method = "RASTER"
            result.notes = "Raster render fast; H3 not needed for this resolution"
        else:
            result.recommended_method = "H3" if result.h3_convert_time_s < result.raster_render_time_s else "RASTER"
            result.notes = "Both approaches viable; choose based on interaction needs"

    except Exception as e:
        result.error = str(e)

    return result


# ─── Multi-frame test (15 days) ───────────────────────────────────────────────

async def test_multiframe(client: httpx.AsyncClient, key: str) -> dict:
    """Test loading + rendering for ALL frames (15 days) of a dataset."""
    config = DATASETS[key]

    if config["value_type"] == "netcdf":
        return {"dataset": key, "skipped": True, "reason": "NetCDF format"}

    t_total = time.time()
    items = await stac_search(client, config["collection"], TEST_BBOX, START_DATE, END_DATE)

    if not items:
        return {"dataset": key, "error": "No items found", "total_time_s": time.time() - t_total}

    frame_times = []
    h3_times = []
    raster_times = []
    total_hexagons = 0

    for item in items[:15]:
        href = _get_signed_href(item, config["asset"])
        if not href:
            continue

        t_frame = time.time()
        raster_result = await asyncio.to_thread(load_raster_for_bbox, href, TEST_BBOX)
        if raster_result is None:
            continue

        data, transform, crs = raster_result
        load_time = time.time() - t_frame

        # H3 conversion
        t_h3 = time.time()
        hexagons = await asyncio.to_thread(
            raster_to_h3, data, transform, crs,
            config["h3_res"], config["filter"], config["classify"],
        )
        h3_time = time.time() - t_h3
        total_hexagons += len(hexagons)

        # Raster render
        t_raster = time.time()
        await asyncio.to_thread(
            raster_to_tile_response, data, transform, TEST_BBOX, config.get("colormap"),
        )
        raster_time = time.time() - t_raster

        frame_times.append(load_time + h3_time)
        h3_times.append(h3_time)
        raster_times.append(raster_time)

    total_time = time.time() - t_total
    return {
        "dataset": key,
        "frames_processed": len(frame_times),
        "total_time_s": total_time,
        "total_load_s": sum(frame_times) - sum(h3_times),
        "total_h3_time_s": sum(h3_times),
        "total_raster_time_s": sum(raster_times),
        "avg_h3_per_frame_s": sum(h3_times) / len(h3_times) if h3_times else 0,
        "avg_raster_per_frame_s": sum(raster_times) / len(raster_times) if raster_times else 0,
        "total_hexagons": total_hexagons,
        "passes_30s_total": total_time < 30.0,
        "h3_render_under_5s": sum(h3_times) < 5.0,
        "raster_render_under_5s": sum(raster_times) < 5.0,
    }


# ─── Report Generation ───────────────────────────────────────────────────────

def generate_report(single_results: list[RenderTestResult], multi_results: list[dict]) -> str:
    lines = []
    lines.append("# Planetary Computer Rendering Performance Report")
    lines.append(f"\n**Test Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Test BBox:** {TEST_BBOX} (Portugal, ~0.5° × 0.5°)")
    lines.append(f"**Date Range:** {START_DATE} to {END_DATE} (15 days)")
    lines.append(f"**Thresholds:** Render < 5s (max 10s), Total pipeline < 30s")
    lines.append("")

    # ─── H3 vs Raster comparison ─────────────────────────────────────────
    lines.append("## H3 vs Raster Rendering Comparison")
    lines.append("")
    lines.append("| Dataset | Pixels | H3 Time | H3 Hexagons | GeoJSON KB | Raster Time | PNG KB | Recommended |")
    lines.append("|---------|--------|---------|-------------|-----------|-------------|--------|-------------|")

    for r in single_results:
        if r.error and "NetCDF" not in r.notes:
            lines.append(f"| `{r.dataset}` | - | - | - | - | - | - | ❌ {r.error[:30]} |")
        elif "NetCDF" in (r.notes or ""):
            lines.append(f"| `{r.dataset}` | - | - | - | - | - | - | ⏭️ NetCDF (skip) |")
        else:
            h3_icon = "✅" if r.h3_convert_time_s < RENDER_TARGET else "⚠️" if r.h3_convert_time_s < RENDER_MAX else "❌"
            raster_icon = "✅" if r.raster_render_time_s < RENDER_TARGET else "⚠️" if r.raster_render_time_s < RENDER_MAX else "❌"
            rec = f"**{r.recommended_method}**"
            lines.append(
                f"| `{r.dataset}` | {r.pixels_loaded:,} | "
                f"{h3_icon} {r.h3_convert_time_s:.2f}s | {r.h3_hexagons:,} | "
                f"{r.h3_geojson_size_kb:.0f} | "
                f"{raster_icon} {r.raster_render_time_s:.3f}s | {r.raster_image_size_kb:.0f} | "
                f"{rec} |"
            )

    lines.append("")

    # ─── Multi-frame (15 days) results ────────────────────────────────────
    lines.append("## 15-Day Multi-Frame Pipeline Results")
    lines.append("")
    lines.append("| Dataset | Frames | Total (s) | Load (s) | H3 Total (s) | Raster Total (s) | H3 <5s? | Raster <5s? | Pipeline <30s? |")
    lines.append("|---------|--------|-----------|----------|--------------|-----------------|---------|-------------|----------------|")

    for m in multi_results:
        if m.get("skipped"):
            lines.append(f"| `{m['dataset']}` | - | - | - | - | - | ⏭️ | ⏭️ | ⏭️ |")
            continue
        if m.get("error"):
            lines.append(f"| `{m['dataset']}` | 0 | - | - | - | - | ❌ | ❌ | ❌ |")
            continue

        h3_ok = "✅" if m["h3_render_under_5s"] else "❌"
        raster_ok = "✅" if m["raster_render_under_5s"] else "❌"
        pipeline_ok = "✅" if m["passes_30s_total"] else "❌"
        lines.append(
            f"| `{m['dataset']}` | {m['frames_processed']} | "
            f"{m['total_time_s']:.1f} | {m['total_load_s']:.1f} | "
            f"{m['total_h3_time_s']:.2f} | {m['total_raster_time_s']:.2f} | "
            f"{h3_ok} | {raster_ok} | {pipeline_ok} |"
        )

    lines.append("")

    # ─── Analysis & Recommendations ───────────────────────────────────────
    lines.append("## Analysis & Recommendations")
    lines.append("")

    # Count recommendations
    h3_recommended = [r for r in single_results if r.recommended_method == "H3"]
    raster_recommended = [r for r in single_results if r.recommended_method == "RASTER"]

    lines.append("### Rendering Method Verdict")
    lines.append("")
    lines.append(f"- **H3 recommended for {len(h3_recommended)} datasets:** "
                 + ", ".join(f"`{r.dataset}`" for r in h3_recommended))
    lines.append(f"- **Raster recommended for {len(raster_recommended)} datasets:** "
                 + ", ".join(f"`{r.dataset}`" for r in raster_recommended))
    lines.append("")

    lines.append("### Why H3 (Hexagons)?")
    lines.append("")
    lines.append("- **Pros:** Consistent spatial aggregation, interactive filtering by hexagon, "
                 "multi-dataset overlay alignment, temporal comparison, client-side styling")
    lines.append("- **Cons:** Conversion overhead (0.01–2s), larger GeoJSON payload, "
                 "polygon rendering more complex than image overlay")
    lines.append("- **Best for:** Classification data (fire, land use), coarse resolution (MODIS 500m/1km), "
                 "analytics dashboards, multi-dataset comparison")
    lines.append("")

    lines.append("### Why Raster (Image Overlay)?")
    lines.append("")
    lines.append("- **Pros:** Near-instant rendering (<0.1s), smaller payload (PNG), pixel-perfect precision, "
                 "natural visualization, trivial caching (tile-based)")
    lines.append("- **Cons:** No client-side filtering, re-render needed for colormap changes, "
                 "alignment harder for multi-dataset overlay")
    lines.append("- **Best for:** High-res data (Sentinel-2 10m, Landsat 30m, ESA WorldCover), "
                 "continuous values (NDVI, temperature), background layers")
    lines.append("")

    lines.append("### Hybrid Approach (Recommended)")
    lines.append("")
    lines.append("Use **both** methods depending on context:")
    lines.append("")
    lines.append("1. **Background layer:** Raster overlay (fast, pretty, cached as tiles)")
    lines.append("2. **Interactive layer:** H3 hexagons (clickable, filterable, animatable)")
    lines.append("3. **Multi-day animation:** Pre-render raster tiles per frame, overlay H3 for stats")
    lines.append("")
    lines.append("```")
    lines.append("Single-day view:  Raster tile (0.01–0.1s) + H3 overlay (0.01–2s) = instant")
    lines.append("15-day animation: Pre-cached raster tiles + H3 aggregates per frame")
    lines.append("```")
    lines.append("")

    # ─── Per-category best choices ────────────────────────────────────────
    lines.append("### Best Collection Per Category (with Rendering Method)")
    lines.append("")

    categories = {
        "Fire": ["fire", "fire_s3"],
        "Floods": ["flood_sar", "flood_jrc"],
        "Vegetation": ["vegetation_ndvi", "vegetation_lai", "vegetation_s2", "vegetation_nbar"],
        "Land Use": ["landuse_esa", "landuse_esri"],
        "Weather": ["weather_lst_daily", "weather_lst_8day", "weather_met_office", "weather_snow"],
        "Elevation": ["dem_copernicus"],
    }

    for cat, keys in categories.items():
        cat_results = [r for r in single_results if r.dataset in keys and not r.error]
        if not cat_results:
            lines.append(f"- **{cat}:** No working endpoints")
            continue
        # Sort by total pipeline time
        best = min(cat_results, key=lambda r: r.total_pipeline_time_s)
        lines.append(
            f"- **{cat}:** `{best.dataset}` ({best.description}) — "
            f"render via **{best.recommended_method}** "
            f"({best.h3_convert_time_s:.2f}s H3 / {best.raster_render_time_s:.3f}s raster, "
            f"{best.h3_hexagons} hexagons, {best.frequency})"
        )

    lines.append("")

    # ─── Detailed notes ───────────────────────────────────────────────────
    lines.append("## Detailed Per-Dataset Notes")
    lines.append("")
    for r in single_results:
        icon = "✅" if (r.h3_passes or r.raster_passes) else "❌"
        lines.append(f"### {icon} `{r.dataset}` — {r.description}")
        lines.append("")
        if r.error:
            lines.append(f"- **Error:** {r.error}")
        else:
            lines.append(f"- **Load time:** {r.load_time_s:.2f}s ({r.pixels_loaded:,} pixels, shape {r.raster_shape})")
            lines.append(f"- **H3 conversion:** {r.h3_convert_time_s:.3f}s → {r.h3_hexagons:,} hexagons (res {r.h3_resolution})")
            lines.append(f"- **H3 GeoJSON payload:** {r.h3_geojson_size_kb:.0f} KB")
            lines.append(f"- **Raster render:** {r.raster_render_time_s:.3f}s → {r.raster_image_size_kb:.0f} KB PNG")
            lines.append(f"- **Total pipeline:** {r.total_pipeline_time_s:.2f}s")
            lines.append(f"- **Recommended:** {r.recommended_method}")
        if r.notes:
            lines.append(f"- **Notes:** {r.notes}")
        lines.append("")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("RENDERING PERFORMANCE TEST — H3 vs RASTER")
    print(f"BBox: {TEST_BBOX} | {START_DATE} → {END_DATE}")
    print("=" * 70)
    print()

    async with httpx.AsyncClient() as client:
        # Phase 1: Single-frame tests (all datasets)
        print("─── PHASE 1: Single-frame load + render benchmarks ───")
        single_results = []
        for key in DATASETS:
            print(f"  {key}...", end=" ", flush=True)
            try:
                result = await asyncio.wait_for(test_dataset(client, key), timeout=60)
            except asyncio.TimeoutError:
                result = RenderTestResult(
                    dataset=key,
                    description=DATASETS[key]["description"],
                    frequency=DATASETS[key]["frequency"],
                    error="Timeout (>60s)",
                )
            single_results.append(result)

            if result.error:
                print(f"❌ ({result.error[:40]})")
            else:
                method = result.recommended_method
                print(
                    f"✅ H3={result.h3_convert_time_s:.2f}s ({result.h3_hexagons} hex) | "
                    f"Raster={result.raster_render_time_s:.3f}s | → {method}"
                )

        print()
        print("─── PHASE 2: Multi-frame (15-day) pipeline tests ───")
        # Phase 2: Multi-frame tests (key datasets only — skip slow ones)
        key_datasets = ["fire", "vegetation_ndvi", "vegetation_lai",
                        "weather_lst_daily", "weather_snow", "landuse_esri"]
        multi_results = []
        for key in key_datasets:
            if key not in DATASETS:
                continue
            print(f"  {key} (15 frames)...", end=" ", flush=True)
            try:
                mresult = await asyncio.wait_for(test_multiframe(client, key), timeout=120)
            except asyncio.TimeoutError:
                mresult = {"dataset": key, "error": "Timeout", "total_time_s": 120}
            multi_results.append(mresult)

            if mresult.get("error"):
                print(f"❌ {mresult['error']}")
            elif mresult.get("skipped"):
                print("⏭️ skipped")
            else:
                ok = "✅" if mresult["passes_30s_total"] else "❌"
                print(
                    f"{ok} {mresult['frames_processed']}fr in {mresult['total_time_s']:.1f}s "
                    f"(H3={mresult['total_h3_time_s']:.2f}s, Raster={mresult['total_raster_time_s']:.2f}s)"
                )

        print()
        print("─── GENERATING REPORT ───")
        report = generate_report(single_results, multi_results)

        report_path = os.path.join(os.path.dirname(__file__), "RENDERING_TEST_REPORT.md")
        with open(report_path, "w") as f:
            f.write(report)

        print(f"Report saved to: {report_path}")
        print()
        # Print summary
        for line in report.split("\n")[:60]:
            print(line)


if __name__ == "__main__":
    asyncio.run(main())
