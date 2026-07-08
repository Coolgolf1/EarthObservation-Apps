"""Raster reading and H3 hexagon conversion — shared by datasets and fire routers."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import h3
import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

from .config import DATASET_CONFIG, GDAL_ENV, MAX_H3_INPUT_PIXELS, MAX_WINDOW_PIXELS


# ─── Metadata-only fire pixel counts (no raster data read) ───

def fire_metadata_from_href(href: str) -> dict[str, Any] | None:
    """Read only raster tags to extract per-day fire pixel counts.

    Returns a dict with:
      - dates: list of date strings (e.g. ["2024-06-25", ...])
      - fire_pixels: list of int counts per day
      - cloud_pixels: list of int counts per day
      - land_pixels: list of int counts per day
      - bands_with_fire: list of 1-based band indices that have fire > 0

    Returns None if metadata is not available.
    """
    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                tags = ds.tags()
                fire_pix_str = tags.get("NUMFIREPIXEL") or tags.get("FirePix")
                dates_str = tags.get("Dates") or tags.get("DAYSOFYEAR")
                if not fire_pix_str or not dates_str:
                    return None

                fire_pixels = [int(x.strip()) for x in fire_pix_str.split(",")]
                dates = dates_str.strip().split()

                cloud_str = tags.get("NUMCLOUDPIXEL") or tags.get("CloudPix") or ""
                cloud_pixels = [int(x.strip()) for x in cloud_str.split(",")] if cloud_str else [0] * len(fire_pixels)

                land_str = tags.get("NUMLAND") or ""
                land_pixels = [int(x.strip()) for x in land_str.split(",")] if land_str else [0] * len(fire_pixels)

                bands_with_fire = [i + 1 for i, fp in enumerate(fire_pixels) if fp > 0]

                return {
                    "dates": dates,
                    "fire_pixels": fire_pixels,
                    "cloud_pixels": cloud_pixels,
                    "land_pixels": land_pixels,
                    "bands_with_fire": bands_with_fire,
                    "total_fire": sum(fire_pixels),
                    "band_count": ds.count,
                }
    except Exception as exc:
        print(f"fire_metadata_from_href error: {exc}")
        return None


def _read_h3_source(
    ds: rasterio.DatasetReader,
    window: Window,
    h3_res: int,
    band: int = 1,
) -> tuple[np.ndarray, "rasterio.Affine"]:
    """Read ``band`` over the full window, downsampling via ``out_shape``.

    Downsampling respects both the H3 cell size (skip reading finer than the
    target hexagon) and the ``MAX_WINDOW_PIXELS`` memory cap, while preserving
    full geographic coverage (the window is never shrunk). Returns the masked
    array as float32 with NaN for nodata, plus the matching affine transform.
    """
    out_h, out_w = int(window.height), int(window.width)

    shape = target_read_shape(window, ds, h3_res)
    if shape:
        out_h, out_w = shape

    total = out_h * out_w
    if total > MAX_WINDOW_PIXELS:
        scale = math.sqrt(MAX_WINDOW_PIXELS / total)
        out_h = max(1, int(out_h * scale))
        out_w = max(1, int(out_w * scale))

    if out_h < window.height or out_w < window.width:
        band_arr = ds.read(
            band, window=window, out_shape=(out_h, out_w),
            masked=True, resampling=rasterio.enums.Resampling.nearest,
        ).astype("float32")
        win_transform = rasterio.windows.transform(window, ds.transform)
        win_transform = rasterio.Affine(
            win_transform.a * (window.width / out_w),
            win_transform.b, win_transform.c,
            win_transform.d,
            win_transform.e * (window.height / out_h),
            win_transform.f,
        )
    else:
        band_arr = ds.read(band, window=window, masked=True).astype("float32")
        win_transform = rasterio.windows.transform(window, ds.transform)

    arr = np.where(band_arr.mask, np.nan, band_arr.data)
    return arr, win_transform


def _aggregate_pixels_to_h3(
    arr: np.ndarray,
    win_transform: "rasterio.Affine",
    ds: rasterio.DatasetReader,
    bbox: list[float],
    h3_res: int,
    filter_fn,
    classify_fn,
) -> list[dict[str, Any]]:
    """Filter pixels, project to WGS84, and aggregate into H3 hexagons.

    Shared by ``raster_to_h3`` and ``raster_to_h3_band``.
    """
    valid_mask = ~np.isnan(arr)
    data_mask = np.zeros_like(arr, dtype=bool)
    data_mask[valid_mask] = filter_fn(arr[valid_mask])

    positions = np.argwhere(data_mask)
    if positions.size == 0:
        return []

    if len(positions) > MAX_H3_INPUT_PIXELS:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(positions), MAX_H3_INPUT_PIXELS, replace=False)
        idx.sort()
        positions = positions[idx]

    rows, cols = positions[:, 0], positions[:, 1]
    xs = cols * win_transform.a + rows * win_transform.b + win_transform.c
    ys = cols * win_transform.d + rows * win_transform.e + win_transform.f

    if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
        from rasterio.warp import transform as warp_transform
        xs_list, ys_list = warp_transform(ds.crs, "EPSG:4326", xs.tolist(), ys.tolist())
        xs, ys = np.array(xs_list), np.array(ys_list)

    values = arr[rows, cols]
    valid = (ys >= -90) & (ys <= 90) & (xs >= -180) & (xs <= 180) & ~np.isnan(values)
    xs, ys, values = xs[valid], ys[valid], values[valid]
    if len(values) == 0:
        return []

    cells = [h3.latlng_to_cell(float(ys[i]), float(xs[i]), h3_res) for i in range(len(values))]

    hex_data: dict[str, dict[str, Any]] = {}
    for i, cell in enumerate(cells):
        val = float(values[i])
        cls = classify_fn(val)
        if cell not in hex_data:
            hex_data[cell] = {
                "h3": cell, "pixel_count": 0,
                "value_sum": 0.0, "value_max": -1e9, "value_min": 1e9,
                "classes": defaultdict(int),
            }
        hd = hex_data[cell]
        hd["pixel_count"] += 1
        hd["value_sum"] += val
        hd["value_max"] = max(hd["value_max"], val)
        hd["value_min"] = min(hd["value_min"], val)
        hd["classes"][cls] += 1

    west, south, east, north = bbox
    result = []
    for cell_id, data in hex_data.items():
        n = data["pixel_count"]
        boundary = h3.cell_to_boundary(cell_id)
        center = h3.cell_to_latlng(cell_id)
        # Skip hexagons whose center falls outside the query bbox
        if not (south <= center[0] <= north and west <= center[1] <= east):
            continue
        classes = dict(data["classes"])
        dominant = max(classes, key=classes.get) if classes else "unknown"
        result.append({
            "h3": cell_id, "pixel_count": n,
            "value_mean": round(data["value_sum"] / max(n, 1), 2),
            "value_max": round(data["value_max"], 2),
            "value_min": round(data["value_min"], 2),
            "dominant_class": dominant, "classes": classes,
            "boundary": [[lat, lng] for lat, lng in boundary],
            "center": [center[0], center[1]],
        })
    return result


def raster_to_h3_band(
    href: str,
    bbox: list[float],
    h3_res: int,
    dataset_key: str,
    band: int = 1,
) -> list[dict[str, Any]]:
    """Read a single band from a multi-band raster and convert to H3.

    Used for fire tiles where metadata tells us exactly which band has fire.
    """
    config = DATASET_CONFIG[dataset_key]
    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                window = window_for_bbox(ds, bbox)
                if window is None:
                    return []
                arr, win_transform = _read_h3_source(ds, window, h3_res, band=band)
                return _aggregate_pixels_to_h3(
                    arr, win_transform, ds, bbox, h3_res,
                    config["filter"], config["classify"],
                )
    except Exception as exc:
        print(f"raster_to_h3_band error ({dataset_key}, band {band}): {exc}")
        return []


# ─── Metadata-only H3 (no pixel data read at all) ────────────

def metadata_to_h3(
    href: str,
    bbox: list[float],
    h3_res: int,
    dataset_key: str,
) -> list[dict[str, Any]]:
    """Generate H3 cells from raster metadata only — zero pixel reads.

    For fire tiles: reads tags to get per-day fire/cloud/land counts and
    distributes them as uniform presence across H3 cells that overlap the
    tile's footprint intersected with the query bbox.

    For other datasets: opens the raster to get its geographic bounds,
    creates H3 cells covering the intersection with query bbox, and marks
    them as "data available" with quality metadata.

    Used at H3 res ≤ 5 to provide instant overview without raster I/O.
    """
    west, south, east, north = bbox

    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                tags = ds.tags()

                # Compute tile bbox in WGS84
                tile_bounds = ds.bounds
                if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
                    from rasterio.warp import transform_bounds as tb
                    tile_west, tile_south, tile_east, tile_north = tb(
                        ds.crs, "EPSG:4326",
                        tile_bounds.left, tile_bounds.bottom,
                        tile_bounds.right, tile_bounds.top,
                    )
                else:
                    tile_west, tile_south, tile_east, tile_north = (
                        tile_bounds.left, tile_bounds.bottom,
                        tile_bounds.right, tile_bounds.top,
                    )

                # Intersect tile bounds with query bbox
                int_west = max(west, tile_west)
                int_south = max(south, tile_south)
                int_east = min(east, tile_east)
                int_north = min(north, tile_north)
                if int_west >= int_east or int_south >= int_north:
                    return []

                # Build H3 cells for the intersection area
                poly = [
                    [int_north, int_west], [int_north, int_east],
                    [int_south, int_east], [int_south, int_west],
                    [int_north, int_west],
                ]
                try:
                    cells = list(set(h3.polygon_to_cells(h3.LatLngPoly(poly), h3_res)))
                except Exception:
                    # Fallback grid
                    step = {1: 7.0, 2: 3.0, 3: 1.2, 4: 0.5, 5: 0.2}.get(h3_res, 0.5)
                    cells = []
                    lat = int_south + step / 2
                    while lat < int_north:
                        lon = int_west + step / 2
                        while lon < int_east:
                            cells.append(h3.latlng_to_cell(lat, lon, h3_res))
                            lon += step
                        lat += step
                    cells = list(set(cells))

                if not cells:
                    return []

                # --- Fire-specific metadata ---
                fire_pix_str = tags.get("NUMFIREPIXEL") or tags.get("FirePix")
                cloud_str = tags.get("NUMCLOUDPIXEL") or tags.get("CloudPix")
                land_str = tags.get("NUMLAND")
                total_pixels = ds.width * ds.height

                if fire_pix_str and dataset_key == "fire":
                    fire_pixels = [int(x.strip()) for x in fire_pix_str.split(",")]
                    cloud_pixels = [int(x.strip()) for x in cloud_str.split(",")] if cloud_str else [0] * len(fire_pixels)
                    land_pixels = [int(x.strip()) for x in land_str.split(",")] if land_str else [total_pixels] * len(fire_pixels)

                    total_fire = sum(fire_pixels)
                    if total_fire == 0:
                        return []

                    # Assign aggregate metadata to each cell
                    # Pixel count = total fire pixels / number of cells (distributed)
                    fire_per_cell = max(1, total_fire // max(len(cells), 1))
                    high_count = sum(1 for fp in fire_pixels if fp > 5)
                    nom_count = sum(1 for fp in fire_pixels if 1 <= fp <= 5)
                    low_count = sum(1 for fp in fire_pixels if fp == 1)

                    result = []
                    for cell_id in cells:
                        boundary = h3.cell_to_boundary(cell_id)
                        center = h3.cell_to_latlng(cell_id)
                        # Classify by intensity
                        if total_fire > 50:
                            dominant = "high"
                        elif total_fire > 10:
                            dominant = "nominal"
                        else:
                            dominant = "low"
                        result.append({
                            "h3": cell_id,
                            "pixel_count": fire_per_cell,
                            "value_mean": float(total_fire),
                            "value_max": float(max(fire_pixels)),
                            "value_min": float(min(fp for fp in fire_pixels if fp > 0)) if total_fire > 0 else 0.0,
                            "dominant_class": dominant,
                            "classes": {"high": high_count, "nominal": nom_count, "low": low_count},
                            "boundary": [[la, ln] for la, ln in boundary],
                            "center": [center[0], center[1]],
                            "days_active": sum(1 for fp in fire_pixels if fp > 0),
                            "metadata": {
                                "fire_pixels_total": total_fire,
                                "fire_pixels_per_day": fire_pixels,
                                "cloud_pixels_per_day": cloud_pixels,
                                "land_pixels_total": sum(land_pixels),
                                "tile_pixels": total_pixels,
                                "source": "metadata_only",
                            },
                        })
                    return result

                # --- Generic metadata for other datasets ---
                # Mark cells as "data present" with quality info
                quality_pct = int(tags.get("QAPERCENTGOODQUALITY", "0") or "0")
                pct_land = int(tags.get("PERCENTLAND", "100") or "100")
                long_name = tags.get("long_name", dataset_key)
                scale_factor = tags.get("scale_factor", "1")

                result = []
                for cell_id in cells:
                    boundary = h3.cell_to_boundary(cell_id)
                    center = h3.cell_to_latlng(cell_id)
                    result.append({
                        "h3": cell_id,
                        "pixel_count": 1,
                        "value_mean": 1.0,
                        "value_max": 1.0,
                        "value_min": 1.0,
                        "dominant_class": "present",
                        "classes": {"present": 1},
                        "boundary": [[la, ln] for la, ln in boundary],
                        "center": [center[0], center[1]],
                        "metadata": {
                            "quality_pct": quality_pct,
                            "pct_land": pct_land,
                            "variable": long_name,
                            "scale_factor": scale_factor,
                            "tile_pixels": total_pixels,
                            "source": "metadata_only",
                        },
                    })
                return result

    except Exception as exc:
        print(f"metadata_to_h3 error ({dataset_key}): {exc}")
        return []


def window_for_bbox(
    ds: rasterio.DatasetReader,
    bbox_wgs84: list[float],
) -> Window | None:
    """Compute the full rasterio Window covering a WGS84 bbox intersection.

    The window is NOT shrunk to cap pixels — shrinking would change the
    geographic coverage (reading only the top-left corner). The memory cap is
    enforced at read time via ``out_shape`` downsampling in ``_read_h3_source``,
    which keeps full coverage while bounding the array size.
    """
    west, south, east, north = bbox_wgs84
    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
    )
    candidate = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
    try:
        bounded = candidate.intersection(Window(0, 0, ds.width, ds.height))
    except Exception:
        # Intersection is empty — bbox doesn't overlap this raster tile
        return None
    if bounded.width <= 0 or bounded.height <= 0:
        return None
    bounded = bounded.round_offsets().round_lengths()
    if bounded.width <= 0 or bounded.height <= 0:
        return None
    return bounded


def target_read_shape(
    window: Window,
    ds: rasterio.DatasetReader,
    h3_res: int,
) -> tuple[int, int] | None:
    """Ideal (height, width) to read, matching pixel size to H3 cell size.

    Returns ``None`` if native resolution is already coarse enough.
    """
    h3_approx_deg = {
        1: 7.0, 2: 3.0, 3: 1.2, 4: 0.5, 5: 0.2,
        6: 0.07, 7: 0.03, 8: 0.01, 9: 0.004,
    }
    target_deg = h3_approx_deg.get(h3_res)
    if target_deg is None:
        return None
    px_deg = abs(ds.transform.a)
    if px_deg <= 0 or target_deg <= 0:
        return None
    ideal_factor = target_deg / px_deg / 2.0
    if ideal_factor < 1.5:
        return None
    out_w = max(1, int(window.width / ideal_factor))
    out_h = max(1, int(window.height / ideal_factor))
    if out_w >= window.width and out_h >= window.height:
        return None
    return (out_h, out_w)


# ─── Preview mode: one-sample-per-hex for large areas ────────

def _raster_preview_h3(
    href: str,
    bbox: list[float],
    h3_res: int,
    dataset_key: str,
) -> list[dict[str, Any]]:
    """Sample one pixel per H3 cell center — fast presence detection for large areas."""
    config = DATASET_CONFIG[dataset_key]
    filter_fn = config["filter"]
    classify_fn = config["classify"]

    west, south, east, north = bbox

    # Build the set of H3 cells that tile this bbox
    bbox_polygon = [
        [north, west], [north, east], [south, east], [south, west], [north, west]
    ]
    try:
        cells = list(h3.polygon_to_cells(
            h3.LatLngPoly(bbox_polygon), h3_res
        ))
    except Exception:
        # Fallback: grid of points
        cells = []
        step = {1: 7.0, 2: 3.0, 3: 1.2}.get(h3_res, 1.0)
        lat = south + step / 2
        while lat < north:
            lon = west + step / 2
            while lon < east:
                cells.append(h3.latlng_to_cell(lat, lon, h3_res))
                lon += step
            lat += step

    if not cells:
        return []

    # Deduplicate
    cells = list(set(cells))

    # Get center coords for each cell
    centers = [h3.cell_to_latlng(c) for c in cells]  # (lat, lon)

    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                # Convert cell centers to the raster's CRS for sampling
                lons = [c[1] for c in centers]
                lats = [c[0] for c in centers]

                if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
                    from rasterio.warp import transform as warp_transform
                    xs, ys = warp_transform("EPSG:4326", ds.crs, lons, lats)
                else:
                    xs, ys = lons, lats

                # Sample raster at each point (returns generator of arrays)
                coords = list(zip(xs, ys))
                samples = list(ds.sample(coords, indexes=1))

                result = []
                for i, (cell_id, (lat, lon)) in enumerate(zip(cells, centers)):
                    val = float(samples[i][0]) if len(samples[i]) > 0 else float("nan")
                    if np.isnan(val):
                        continue

                    # Check if pixel passes the dataset filter
                    arr_check = np.array([val])
                    if not filter_fn(arr_check)[0]:
                        continue

                    cls = classify_fn(val)
                    boundary = h3.cell_to_boundary(cell_id)
                    result.append({
                        "h3": cell_id,
                        "pixel_count": 1,
                        "value_mean": round(val, 2),
                        "value_max": round(val, 2),
                        "value_min": round(val, 2),
                        "dominant_class": cls,
                        "classes": {cls: 1},
                        "boundary": [[la, ln] for la, ln in boundary],
                        "center": [lat, lon],
                    })

                return result
    except Exception as exc:
        print(f"Raster preview error ({dataset_key}): {exc}")
        return []


# ─── Full mode: pixel-level aggregation ──────────────────────

def raster_to_h3(
    href: str,
    bbox: list[float],
    h3_res: int,
    dataset_key: str,
) -> list[dict[str, Any]]:
    """Read a raster, find matching pixels, aggregate into H3 hexagons.

    At low resolution (h3_res <= 5 — overview), uses metadata-only mode:
    reads raster tags to determine presence/counts without pixel I/O.
    At higher resolution (>= 6), does full pixel-level aggregation.
    """
    # Metadata-only mode: no pixel reading, just tags
    if h3_res <= 5:
        return metadata_to_h3(href, bbox, h3_res, dataset_key)

    config = DATASET_CONFIG[dataset_key]
    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                window = window_for_bbox(ds, bbox)
                if window is None:
                    return []
                arr, win_transform = _read_h3_source(ds, window, h3_res, band=1)
                return _aggregate_pixels_to_h3(
                    arr, win_transform, ds, bbox, h3_res,
                    config["filter"], config["classify"],
                )
    except Exception as exc:
        print(f"Raster H3 error ({dataset_key}): {exc}")
        return []
