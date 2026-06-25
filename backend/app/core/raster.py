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


def window_for_bbox(
    ds: rasterio.DatasetReader,
    bbox_wgs84: list[float],
    max_pixels: int = MAX_WINDOW_PIXELS,
) -> Window | None:
    """Compute a rasterio Window for a WGS84 bbox, capping pixel count."""
    west, south, east, north = bbox_wgs84
    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
    )
    candidate = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
    bounded = candidate.intersection(Window(0, 0, ds.width, ds.height))
    if bounded.width <= 0 or bounded.height <= 0:
        return None
    bounded = bounded.round_offsets().round_lengths()
    total_px = bounded.width * bounded.height
    if total_px > max_pixels:
        scale = math.sqrt(max_pixels / total_px)
        bounded = Window(
            col_off=bounded.col_off,
            row_off=bounded.row_off,
            width=max(1, int(bounded.width * scale)),
            height=max(1, int(bounded.height * scale)),
        )
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

    At low resolution (h3_res <= 3 — large areas), uses preview mode:
    samples one pixel at each hex cell center for fast presence detection.
    At higher resolution (>= 4), does full pixel-level aggregation.
    """
    # Preview mode: one sample per hex cell
    if h3_res <= 3:
        return _raster_preview_h3(href, bbox, h3_res, dataset_key)

    config = DATASET_CONFIG[dataset_key]
    filter_fn = config["filter"]
    classify_fn = config["classify"]

    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(href) as ds:
                window = window_for_bbox(ds, bbox)
                if window is None:
                    return []

                shape = target_read_shape(window, ds, h3_res)
                if shape:
                    out_h, out_w = shape
                    band = ds.read(
                        1, window=window, out_shape=(out_h, out_w),
                        masked=True,
                        resampling=rasterio.enums.Resampling.nearest,
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
                    band = ds.read(1, window=window, masked=True).astype("float32")
                    win_transform = rasterio.windows.transform(window, ds.transform)

                arr = np.where(band.mask, np.nan, band.data)

                valid_mask = ~np.isnan(arr)
                data_mask = np.zeros_like(arr, dtype=bool)
                data_mask[valid_mask] = filter_fn(arr[valid_mask])

                positions = np.argwhere(data_mask)
                if positions.size == 0:
                    return []

                if len(positions) > MAX_H3_INPUT_PIXELS:
                    rng = np.random.default_rng(42)
                    idx = rng.choice(len(positions), pixel_cap, replace=False)
                    idx.sort()
                    positions = positions[idx]

                rows, cols = positions[:, 0], positions[:, 1]
                xs = cols * win_transform.a + rows * win_transform.b + win_transform.c
                ys = cols * win_transform.d + rows * win_transform.e + win_transform.f

                if str(ds.crs).upper() not in ("EPSG:4326", "WGS84", "OGC:CRS84"):
                    from rasterio.warp import transform as warp_transform
                    xs_list, ys_list = warp_transform(
                        ds.crs, "EPSG:4326", xs.tolist(), ys.tolist()
                    )
                    xs, ys = np.array(xs_list), np.array(ys_list)

                values = arr[rows, cols]
                valid = (
                    (ys >= -90) & (ys <= 90)
                    & (xs >= -180) & (xs <= 180)
                    & ~np.isnan(values)
                )
                xs, ys, values = xs[valid], ys[valid], values[valid]
                if len(values) == 0:
                    return []

                cells = [
                    h3.latlng_to_cell(float(ys[i]), float(xs[i]), h3_res)
                    for i in range(len(values))
                ]

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
                    hd = hex_data[cell]
                    hd["pixel_count"] += 1
                    hd["value_sum"] += val
                    hd["value_max"] = max(hd["value_max"], val)
                    hd["value_min"] = min(hd["value_min"], val)
                    hd["classes"][cls] += 1

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
