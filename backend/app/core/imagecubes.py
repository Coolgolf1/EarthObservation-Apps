"""
ImageCubes API integration for Sentinel-2 NDVI.

Creates or reuses Sentinel-2 datacubes via the ImageCubes API,
then reads the resulting Zarr and computes NDVI from B04 (Red) and B08 (NIR).
Aggregates pixels into H3 hexagons.

Requires IMAGECUBES_TOKEN environment variable with a valid OAuth2 bearer token.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import date
from typing import Any

import h3
import httpx
import numpy as np
import xarray as xr

IMAGECUBES_API = "https://imagecube-toucan-api.jollyground-fbd4d1eb.eastus.azurecontainerapps.io"

# ─── Memory Safety ────────────────────────────────────────────
# Machine has ~6.6 GB total RAM (~5.9 GB usable). Cap per-request loads.
MAX_IMAGECUBES_BYTES = 1_000_000_000  # 1 GB max per ImageCubes load


def _get_token() -> str:
    token = os.environ.get("IMAGECUBES_TOKEN", "")
    if not token:
        raise RuntimeError(
            "IMAGECUBES_TOKEN env var not set. "
            "Run the image_cube.py script to authenticate and set the token."
        )
    return token


async def _find_or_create_cube(
    bbox: list[float],
    date_start: date,
    date_end: date,
    token: str,
) -> str:
    """Find an existing cube or create a new one. Returns cube ID."""
    headers = {"Authorization": f"Bearer {token}"}
    west, south, east, north = bbox

    async with httpx.AsyncClient(base_url=IMAGECUBES_API, timeout=30) as client:
        # Check for existing cubes that match
        resp = await client.get("/api/v1/imagecubes", headers=headers)
        resp.raise_for_status()
        cubes = resp.json()

        # Look for a matching cube (same bbox and overlapping dates)
        for cube in cubes if isinstance(cubes, list) else cubes.get("items", []):
            cube_bbox = cube.get("bbox", [])
            if (
                cube.get("status") == "completed"
                and len(cube_bbox) == 4
                and abs(cube_bbox[0] - west) < 0.01
                and abs(cube_bbox[1] - south) < 0.01
                and abs(cube_bbox[2] - east) < 0.01
                and abs(cube_bbox[3] - north) < 0.01
            ):
                return cube["id"]

        # Create a new cube
        payload = {
            "bbox": [west, south, east, north],
            "start_date": date_start.isoformat(),
            "end_date": date_end.isoformat(),
            "interval": "biweekly",
            "bands": ["B04", "B08"],
            "cloud_cover_threshold": 20,
            "blend_method": "median",
            "cloud_mask_mode": "cloud_masked",
            "name": f"ndvi_{west:.2f}_{south:.2f}_{date_start}",
            "description": "NDVI from B04/B08 for EarthAnalyser",
        }
        resp = await client.post("/api/v1/imagecubes", json=payload, headers=headers)
        resp.raise_for_status()
        cube = resp.json()
        return cube["id"]


async def _wait_for_cube(cube_id: str, token: str, max_wait: int = 300) -> dict:
    """Poll until the cube job completes or fails."""
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0
    interval = 5

    async with httpx.AsyncClient(base_url=IMAGECUBES_API, timeout=30) as client:
        while elapsed < max_wait:
            resp = await client.get(f"/api/v1/imagecubes/{cube_id}", headers=headers)
            resp.raise_for_status()
            cube = resp.json()
            status = cube.get("status", "unknown")

            if status == "completed":
                return cube
            elif status in ("failed", "error"):
                raise RuntimeError(f"ImageCube job {cube_id} failed: {cube.get('error', 'unknown')}")

            await asyncio.sleep(interval)
            elapsed += interval
            interval = min(interval * 1.5, 30)

    raise TimeoutError(f"ImageCube job {cube_id} not completed after {max_wait}s")


async def _get_artifact_url(cube_id: str, token: str) -> str:
    """Get the signed URL for the cube artifact (Zarr store)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=IMAGECUBES_API, timeout=30) as client:
        resp = await client.get(f"/api/v1/imagecubes/{cube_id}/artifact-url", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("url") or data.get("artifact_url") or data.get("href", "")


async def imagecubes_ndvi_to_h3(
    bbox: list[float],
    date_start: date,
    date_end: date,
    h3_res: int,
) -> dict[str, Any]:
    """
    Get NDVI from ImageCubes API (Sentinel-2 B04/B08), aggregate to H3.
    Returns dict compatible with the datasets endpoint response format.
    """
    token = _get_token()

    # Find or create the cube, then wait for completion
    cube_id = await _find_or_create_cube(bbox, date_start, date_end, token)
    cube = await _wait_for_cube(cube_id, token)

    # Get the Zarr artifact URL
    artifact_url = await _get_artifact_url(cube_id, token)
    if not artifact_url:
        raise RuntimeError(f"No artifact URL returned for cube {cube_id}")

    # Open the Zarr store
    ds = await asyncio.to_thread(
        xr.open_zarr, artifact_url, consolidated=True, chunks={}
    )

    try:
        # ─── Memory guard: check band sizes before loading ───
        total_bytes = ds["B04"].nbytes + ds["B08"].nbytes
        if total_bytes > MAX_IMAGECUBES_BYTES:
            raise MemoryError(
                f"ImageCubes request too large: {total_bytes / 1e9:.2f} GB estimated "
                f"(limit {MAX_IMAGECUBES_BYTES / 1e9:.1f} GB). "
                f"Reduce bbox or date range."
            )

        # The band .compute() and per-pixel H3 aggregation are CPU/IO-bound and
        # would block the event loop, so run them off the loop in a worker thread.
        frames, aggregate_values = await asyncio.to_thread(
            _compute_ndvi_frames, ds, h3_res, date_start
        )
    finally:
        ds.close()

    return {
        "dataset": "vegetation",
        "collection": "sentinel-2-imagecubes",
        "description": "Sentinel-2 NDVI (ImageCubes Zarr)",
        "frames": frames,
        "hexagons": aggregate_values,
        "total_hexagons": len(aggregate_values),
        "frames_with_data": len(frames),
    }


def _compute_ndvi_frames(
    ds: "xr.Dataset",
    h3_res: int,
    date_start: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute NDVI and aggregate pixels into H3 hexagons (CPU-bound, sync).

    Returns ``(frames, aggregate_hexagons)``. Runs in a worker thread so the
    per-pixel H3 loops never block the async event loop.
    """
    # Read B04 (Red) and B08 (NIR) bands
    b04 = ds["B04"].compute()
    b08 = ds["B08"].compute()

    # Compute NDVI = (B08 - B04) / (B08 + B04)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (b08.values.astype(np.float32) - b04.values.astype(np.float32)) / (
            b08.values.astype(np.float32) + b04.values.astype(np.float32)
        )

    # Get coordinates
    lats = ds.coords["latitude"].values if "latitude" in ds.coords else ds.coords["y"].values
    lons = ds.coords["longitude"].values if "longitude" in ds.coords else ds.coords["x"].values
    times = ds.coords["time"].values if "time" in ds.coords else None

    # If no time dimension, treat as single frame
    if times is None or ndvi.ndim == 2:
        ndvi = ndvi[np.newaxis, ...]  # add time dim
        times = [date_start]

    frames: list[dict[str, Any]] = []
    aggregate_hex: dict[str, dict[str, Any]] = {}

    for t_idx, time_val in enumerate(times):
        if hasattr(time_val, "astype"):
            acq_date = str(np.datetime_as_string(time_val, unit="D"))
        else:
            acq_date = str(time_val)

        grid = ndvi[t_idx]  # shape: (lat, lon)

        day_hex: dict[str, dict[str, Any]] = {}

        for lat_idx, lat_val in enumerate(lats):
            for lon_idx, lon_val in enumerate(lons):
                val = float(grid[lat_idx, lon_idx])
                if np.isnan(val) or val < -1 or val > 1:
                    continue

                cell = h3.latlng_to_cell(float(lat_val), float(lon_val), h3_res)

                if cell not in day_hex:
                    day_hex[cell] = {
                        "h3": cell,
                        "pixel_count": 0,
                        "value_sum": 0.0,
                        "value_max": -1e9,
                        "value_min": 1e9,
                        "classes": defaultdict(int),
                    }

                day_hex[cell]["pixel_count"] += 1
                day_hex[cell]["value_sum"] += val
                day_hex[cell]["value_max"] = max(day_hex[cell]["value_max"], val)
                day_hex[cell]["value_min"] = min(day_hex[cell]["value_min"], val)

                # Classify NDVI
                if val > 0.6:
                    cls = "dense"
                elif val > 0.3:
                    cls = "moderate"
                elif val > 0.1:
                    cls = "sparse"
                else:
                    cls = "bare"
                day_hex[cell]["classes"][cls] += 1

        # Finalize hexagons
        day_hexagons = []
        for cell_id, data in day_hex.items():
            n = data["pixel_count"]
            boundary = h3.cell_to_boundary(cell_id)
            center = h3.cell_to_latlng(cell_id)
            classes = dict(data["classes"])
            dominant = max(classes, key=classes.get) if classes else "unknown"

            hex_entry = {
                "h3": cell_id,
                "pixel_count": n,
                "value_mean": round(data["value_sum"] / max(n, 1), 4),
                "value_max": round(data["value_max"], 4),
                "value_min": round(data["value_min"], 4),
                "dominant_class": dominant,
                "classes": classes,
                "boundary": [[lat, lng] for lat, lng in boundary],
                "center": [center[0], center[1]],
            }
            day_hexagons.append(hex_entry)

            # Accumulate aggregate
            if cell_id not in aggregate_hex:
                aggregate_hex[cell_id] = {
                    **hex_entry,
                    "days_active": 0,
                    "_value_sum": 0.0,
                    "_value_count": 0,
                }
            agg = aggregate_hex[cell_id]
            agg["days_active"] = agg.get("days_active", 0) + 1
            agg["_value_sum"] = agg.get("_value_sum", 0.0) + hex_entry["value_mean"] * n
            agg["_value_count"] = agg.get("_value_count", 0) + n
            if hex_entry["value_max"] > agg["value_max"]:
                agg["value_max"] = hex_entry["value_max"]
            if hex_entry["value_min"] < agg["value_min"]:
                agg["value_min"] = hex_entry["value_min"]
            for cls, cnt in classes.items():
                agg["classes"][cls] = agg["classes"].get(cls, 0) + cnt
            agg["dominant_class"] = max(agg["classes"], key=agg["classes"].get)

        if day_hexagons:
            frames.append({"acq_date": acq_date, "hexagons": day_hexagons})

    # Finalize aggregate means
    for cell_id, agg in aggregate_hex.items():
        if agg.get("_value_count", 0) > 0:
            agg["value_mean"] = round(agg["_value_sum"] / agg["_value_count"], 4)
        agg.pop("_value_sum", None)
        agg.pop("_value_count", None)

    return frames, list(aggregate_hex.values())
