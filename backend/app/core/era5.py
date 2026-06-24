"""
ERA5 Zarr weather data access.

Reads 2m temperature from the ERA5 Zarr store on Azure Blob Storage
(Planetary Computer), aggregates into H3 hexagons.

Each monthly Zarr is at: abfs://era5/ERA5/{year}/{month:02d}/air_temperature_at_2_metres.zarr
Grid: lat 90 to -90 (step -0.25, 721 pts), lon 0 to 359.75 (step 0.25, 1440 pts)
Units: Kelvin. Temporal resolution: hourly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import h3
import numpy as np
import xarray as xr


ERA5_STORAGE_OPTIONS = {"account_name": "cpdataeuwest"}
ERA5_VAR = "air_temperature_at_2_metres"

# Cache open datasets per month to avoid re-opening
_month_cache: dict[str, xr.Dataset] = {}


def _zarr_url(year: int, month: int) -> str:
    return f"abfs://era5/ERA5/{year}/{month:02d}/{ERA5_VAR}.zarr"


def _open_month(year: int, month: int) -> xr.Dataset:
    key = f"{year}-{month:02d}"
    if key not in _month_cache:
        url = _zarr_url(year, month)
        _month_cache[key] = xr.open_zarr(
            url,
            storage_options=ERA5_STORAGE_OPTIONS,
            consolidated=True,
            chunks={},
        )
    return _month_cache[key]


def era5_to_h3(
    bbox: list[float],
    date_start: date,
    date_end: date,
    h3_res: int,
) -> dict[str, Any]:
    """
    Read ERA5 2m temperature for a bbox/date range, aggregate to H3 hexagons.
    Returns dict compatible with the datasets endpoint response format.
    """
    west, south, east, north = bbox

    # Determine which months we need
    months_needed: list[tuple[int, int]] = []
    cur = date_start.replace(day=1)
    while cur <= date_end:
        months_needed.append((cur.year, cur.month))
        # Move to next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    # Open and concat monthly Zarr stores
    monthly_arrays: list[xr.DataArray] = []
    for year, month in months_needed:
        ds = _open_month(year, month)
        da = ds[ERA5_VAR]
        monthly_arrays.append(da)

    if not monthly_arrays:
        return {
            "dataset": "weather",
            "collection": "era5-pds",
            "description": "ERA5 2m Temperature (Zarr)",
            "frames": [],
            "hexagons": [],
            "total_hexagons": 0,
            "frames_with_data": 0,
        }

    # Concat along time if multiple months
    if len(monthly_arrays) == 1:
        da = monthly_arrays[0]
    else:
        da = xr.concat(monthly_arrays, dim="time")

    # ERA5 grid: lat 90→-90 (descending), lon 0→359.75
    # Convert bbox lon to 0-360 range
    lon_west = west % 360
    lon_east = east % 360

    # Spatial slice
    if lon_west <= lon_east:
        lon_slice = slice(lon_west, lon_east)
    else:
        # Wraps dateline — rare, just take full range
        lon_slice = slice(0, 360)

    # lat is descending (90 to -90), so slice north→south
    lat_slice = slice(north, south)

    # Time slice
    time_slice = slice(str(date_start), str(date_end))

    subset = da.sel(lon=lon_slice, lat=lat_slice, time=time_slice)

    # Resample to daily mean (ERA5 is hourly)
    daily = subset.resample(time="1D").mean()

    # Load into memory
    daily_data = daily.compute()

    lats = daily_data.coords["lat"].values
    lons = daily_data.coords["lon"].values
    times = daily_data.coords["time"].values

    # Convert lon from 0-360 to -180..180 for H3
    lons_h3 = np.where(lons > 180, lons - 360, lons)

    # Build frames per day and aggregate hexagons
    frames: list[dict[str, Any]] = []
    aggregate_hex: dict[str, dict[str, Any]] = {}

    for t_idx, time_val in enumerate(times):
        acq_date = str(np.datetime_as_string(time_val, unit="D"))
        grid = daily_data.values[t_idx]  # shape: (lat, lon)

        # Convert Kelvin to Celsius
        grid_celsius = grid - 273.15

        # Assign to H3 cells
        day_hex: dict[str, dict[str, Any]] = {}

        for lat_idx, lat_val in enumerate(lats):
            for lon_idx, lon_val in enumerate(lons_h3):
                val = float(grid_celsius[lat_idx, lon_idx])
                if np.isnan(val):
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

                # Classify temperature
                if val > 40:
                    cls = "extreme"
                elif val > 30:
                    cls = "hot"
                elif val > 20:
                    cls = "warm"
                else:
                    cls = "cool"
                day_hex[cell]["classes"][cls] += 1

        # Finalize day hexagons
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
                "value_mean": round(data["value_sum"] / max(n, 1), 2),
                "value_max": round(data["value_max"], 2),
                "value_min": round(data["value_min"], 2),
                "dominant_class": dominant,
                "classes": classes,
                "boundary": [[lat, lng] for lat, lng in boundary],
                "center": [center[0], center[1]],
            }
            day_hexagons.append(hex_entry)

            # Accumulate into aggregate
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
            agg["value_mean"] = round(agg["_value_sum"] / agg["_value_count"], 2)
        agg.pop("_value_sum", None)
        agg.pop("_value_count", None)

    return {
        "dataset": "weather",
        "collection": "era5-pds",
        "description": "ERA5 2m Temperature (Zarr)",
        "frames": frames,
        "hexagons": list(aggregate_hex.values()),
        "total_hexagons": len(aggregate_hex),
        "frames_with_data": len(frames),
    }
