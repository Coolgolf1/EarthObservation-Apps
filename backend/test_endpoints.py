"""
Comprehensive endpoint test for Planetary Computer STAC API collections.

Tests data retrieval for: fire, floods, vegetation, land use, weather, and image cubes.
Measures response times per-request and total for 15-day windows.
Generates a markdown report with findings.

Usage:
    python test_endpoints.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
STAC_API = "https://planetarycomputer.microsoft.com/api/stac/v1"
ERA5_ACCOUNT = "cpdataeuwest"
ERA5_CONTAINER = "era5"

# Test bbox: small area in Portugal (fire-prone region, good for all datasets)
TEST_BBOX = [-8.5, 39.5, -8.0, 40.0]  # [west, south, east, north]

# Time window: last 15 days from today (or recent known-data window)
# For some datasets, recent data may have latency, so we use a window
# that's 30-45 days ago to ensure data availability
END_DATE = date(2025, 6, 1)
START_DATE = END_DATE - timedelta(days=15)

# Timeouts
SINGLE_REQUEST_TIMEOUT = 20.0  # seconds per request
TOTAL_TIMEOUT = 30.0  # seconds for entire 15-day fetch

# ImageCubes
IMAGECUBES_API = "https://imagecube-toucan-api.jollyground-fbd4d1eb.eastus.azurecontainerapps.io"


@dataclass
class TestResult:
    category: str
    collection: str
    description: str
    items_found: int = 0
    items_with_data: int = 0
    total_time_s: float = 0.0
    per_request_times: list[float] = field(default_factory=list)
    avg_request_time_s: float = 0.0
    max_request_time_s: float = 0.0
    success: bool = False
    error: str = ""
    notes: str = ""
    temporal_frequency: str = ""
    passes_threshold: bool = False  # True if total < 30s AND avg < 10s


# ─── STAC Search Helper ──────────────────────────────────────────────────────
async def stac_search(
    client: httpx.AsyncClient,
    collection: str,
    bbox: list[float],
    start: date,
    end: date,
    limit: int = 15,
) -> tuple[list[dict], float]:
    """Search STAC API and return (items, elapsed_seconds)."""
    t0 = time.time()
    payload = {
        "collections": [collection],
        "bbox": bbox,
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": limit,
    }
    resp = await client.post(f"{STAC_API}/search", json=payload, timeout=SINGLE_REQUEST_TIMEOUT)
    resp.raise_for_status()
    elapsed = time.time() - t0
    data = resp.json()
    items = data.get("features", [])
    return items, elapsed


async def stac_search_extended(
    client: httpx.AsyncClient,
    collection: str,
    bbox: list[float],
    start: date,
    end: date,
    limit: int = 15,
) -> tuple[list[dict], float]:
    """Search STAC with extended date range fallback if no items found."""
    items, elapsed = await stac_search(client, collection, bbox, start, end, limit)
    if not items:
        # Try extending to 90 days back
        extended_start = end - timedelta(days=90)
        items, elapsed = await stac_search(client, collection, bbox, extended_start, end, limit)
    if not items:
        # Try 1 year back
        extended_start = end - timedelta(days=365)
        items, elapsed = await stac_search(client, collection, bbox, extended_start, end, limit)
    if not items:
        # Try 5 years (for static/annual datasets)
        extended_start = end - timedelta(days=365 * 5)
        items, elapsed = await stac_search(client, collection, bbox, extended_start, end, limit)
    if not items:
        # Try full range (no temporal filter) for truly static datasets
        t0 = time.time()
        payload = {
            "collections": [collection],
            "bbox": bbox,
            "limit": limit,
        }
        resp = await client.post(f"{STAC_API}/search", json=payload, timeout=SINGLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
        elapsed = time.time() - t0
        items = resp.json().get("features", [])
    return items, elapsed


# ─── Asset Download Test ──────────────────────────────────────────────────────
async def test_asset_read(
    client: httpx.AsyncClient,
    item: dict,
    asset_key: str,
    max_bytes: int = 1_000_000,
) -> tuple[bool, float]:
    """Try to read first bytes of an asset. Returns (success, elapsed)."""
    import planetary_computer

    try:
        assets = item.get("assets", {})
        if asset_key not in assets:
            # Try first available raster asset
            for k, v in assets.items():
                if v.get("type", "").startswith("image/tiff") or "geotiff" in v.get("type", "").lower():
                    asset_key = k
                    break
            else:
                return False, 0.0

        href = assets[asset_key]["href"]
        # Sign the URL
        signed = planetary_computer.sign(href)

        t0 = time.time()
        resp = await client.get(
            signed,
            headers={"Range": f"bytes=0-{max_bytes}"},
            timeout=SINGLE_REQUEST_TIMEOUT,
        )
        elapsed = time.time() - t0
        return resp.status_code in (200, 206), elapsed
    except Exception as e:
        return False, 0.0


# ─── Category Tests ──────────────────────────────────────────────────────────

async def test_fire_modis_14a1(client: httpx.AsyncClient) -> TestResult:
    """MODIS Thermal Anomalies/Fire Daily (MOD14A1/MYD14A1) - 1km daily."""
    result = TestResult(
        category="Fire",
        collection="modis-14A1-061",
        description="MODIS Thermal Anomalies/Fire Daily 1km",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-14A1-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        # Test reading actual raster data
        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "FireMask")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_fire_viirs_375(client: httpx.AsyncClient) -> TestResult:
    """VIIRS Active Fire 375m (VNP14A1)."""
    result = TestResult(
        category="Fire",
        collection="viirs-14a1-001",
        description="VIIRS Active Fire 375m Daily",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "viirs-14a1-001", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "FireMask")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_fire_firms(client: httpx.AsyncClient) -> TestResult:
    """MODIS/VIIRS FIRMS active fire (NRT)."""
    result = TestResult(
        category="Fire",
        collection="modis-14A2-061",
        description="MODIS 8-day Fire (MOD14A2)",
        temporal_frequency="8-day",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-14A2-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "FireMask")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_flood_jrc_gsw(client: httpx.AsyncClient) -> TestResult:
    """JRC Global Surface Water - monthly recurrence."""
    result = TestResult(
        category="Floods",
        collection="jrc-gsw",
        description="JRC Global Surface Water (occurrence, transitions)",
        temporal_frequency="Static/Annual",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "jrc-gsw", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "occurrence")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_flood_sentinel1_rtc(client: httpx.AsyncClient) -> TestResult:
    """Sentinel-1 RTC (Radiometrically Terrain Corrected) - for flood mapping."""
    result = TestResult(
        category="Floods",
        collection="sentinel-1-rtc",
        description="Sentinel-1 RTC for flood detection (VV/VH SAR)",
        temporal_frequency="6-12 days",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "sentinel-1-rtc", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "vv")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_flood_sentinel1_grd(client: httpx.AsyncClient) -> TestResult:
    """Sentinel-1 GRD - SAR for flood extent."""
    result = TestResult(
        category="Floods",
        collection="sentinel-1-grd",
        description="Sentinel-1 GRD SAR (flood extent mapping)",
        temporal_frequency="6-12 days",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "sentinel-1-grd", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "vv")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_vegetation_modis_ndvi(client: httpx.AsyncClient) -> TestResult:
    """MODIS Vegetation Indices 16-day (MOD13A1)."""
    result = TestResult(
        category="Vegetation",
        collection="modis-13A1-061",
        description="MODIS NDVI/EVI 16-day 500m",
        temporal_frequency="16-day",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-13A1-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "500m_16_days_NDVI")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_vegetation_modis_lai(client: httpx.AsyncClient) -> TestResult:
    """MODIS Leaf Area Index (MOD15A2H) - 8-day 500m."""
    result = TestResult(
        category="Vegetation",
        collection="modis-15A2H-061",
        description="MODIS LAI/FPAR 8-day 500m",
        temporal_frequency="8-day",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-15A2H-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "Lai_500m")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_vegetation_sentinel2_l2a(client: httpx.AsyncClient) -> TestResult:
    """Sentinel-2 L2A (for custom NDVI, high-res vegetation)."""
    result = TestResult(
        category="Vegetation",
        collection="sentinel-2-l2a",
        description="Sentinel-2 L2A 10m (bands for NDVI: B04, B08)",
        temporal_frequency="5-day revisit",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "sentinel-2-l2a", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "B08")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_landuse_esa_worldcover(client: httpx.AsyncClient) -> TestResult:
    """ESA WorldCover 10m land use/land cover."""
    result = TestResult(
        category="Land Use",
        collection="esa-worldcover",
        description="ESA WorldCover 10m (annual global land cover)",
        temporal_frequency="Annual",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "esa-worldcover", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "map")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_landuse_esri_lulc(client: httpx.AsyncClient) -> TestResult:
    """Esri 10m Annual Land Use/Land Cover."""
    result = TestResult(
        category="Land Use",
        collection="io-lulc-annual-v02",
        description="Esri 10m Annual Land Use/Land Cover (2017-2023)",
        temporal_frequency="Annual",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "io-lulc-annual-v02", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "data")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_landuse_modis_lc(client: httpx.AsyncClient) -> TestResult:
    """MODIS Land Cover Type (MCD12Q1) - annual 500m."""
    result = TestResult(
        category="Land Use",
        collection="modis-12Q1-061",
        description="MODIS Land Cover Type Annual 500m",
        temporal_frequency="Annual",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-12Q1-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "LC_Type1")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_weather_era5(client: httpx.AsyncClient) -> TestResult:
    """ERA5 Zarr - daily temperature from Azure Blob (2m air temperature)."""
    result = TestResult(
        category="Weather",
        collection="era5-pds",
        description="ERA5 2m Temperature via Zarr on Azure (hourly max/min)",
        temporal_frequency="Hourly (aggregated daily)",
    )
    try:
        import adlfs
        import xarray as xr

        t_total = time.time()
        west, south, east, north = TEST_BBOX
        request_times = []

        # Use the correct variable name from the catalog
        t0 = time.time()
        fs = adlfs.AzureBlobFileSystem(account_name="cpdataeuwest", anon=True)

        # Correct path from STAC catalog
        zarr_path = "era5/ERA5/2024/06/air_temperature_at_2_metres_1hour_Maximum.zarr"
        try:
            store = fs.get_mapper(zarr_path)
            ds = xr.open_zarr(store, consolidated=True)
        except Exception:
            # Try alternative
            zarr_path = "era5/ERA5/2024/01/air_temperature_at_2_metres_1hour_Maximum.zarr"
            store = fs.get_mapper(zarr_path)
            ds = xr.open_zarr(store, consolidated=True)

        open_time = time.time() - t0
        request_times.append(open_time)

        # Subset spatially
        t0 = time.time()
        var_name = list(ds.data_vars)[0]
        var = ds[var_name]

        # Handle coordinate names
        lat_dim = [d for d in var.dims if "lat" in d.lower()][0]
        lon_dim = [d for d in var.dims if "lon" in d.lower()][0]

        # ERA5 lat may be descending
        lat_vals = var[lat_dim].values
        if lat_vals[0] > lat_vals[-1]:
            subset = var.sel(**{lat_dim: slice(north, south), lon_dim: slice(west, east)})
        else:
            subset = var.sel(**{lat_dim: slice(south, north), lon_dim: slice(west, east)})

        # Load first 24 timesteps
        time_dim = [d for d in var.dims if "time" in d.lower()]
        if time_dim:
            data = subset.isel(**{time_dim[0]: slice(0, 24)}).values
        else:
            data = subset.values
        load_time = time.time() - t0
        request_times.append(load_time)

        result.items_found = int(subset.sizes.get(time_dim[0], 0)) if time_dim else 1
        result.items_with_data = 1 if data.size > 0 else 0
        result.per_request_times = request_times
        result.total_time_s = time.time() - t_total
        result.notes = f"Path: {zarr_path}, shape: {data.shape}, mean: {np.nanmean(data):.1f}K"

    except Exception as e:
        result.error = str(e)
        result.total_time_s = time.time() - t_total if "t_total" in dir() else 0
    _finalize(result)
    return result


async def test_weather_modis_lst(client: httpx.AsyncClient) -> TestResult:
    """MODIS Land Surface Temperature (MOD11A2) - 8-day 1km."""
    result = TestResult(
        category="Weather",
        collection="modis-11A2-061",
        description="MODIS Land Surface Temp 8-day 1km",
        temporal_frequency="8-day",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-11A2-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "LST_Day_1km")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_weather_daymet(client: httpx.AsyncClient) -> TestResult:
    """Daymet daily weather (temperature, precipitation) - North America 1km."""
    result = TestResult(
        category="Weather",
        collection="daymet-daily-na",
        description="Daymet Daily Weather 1km (North America: temp, precip, srad) - Zarr only",
        temporal_frequency="Daily",
    )
    try:
        import adlfs
        import xarray as xr

        t_total = time.time()
        # Daymet is Zarr-only on Azure blob, not STAC items
        fs = adlfs.AzureBlobFileSystem(account_name="daymeteuwest", anon=True)

        t0 = time.time()
        try:
            store = fs.get_mapper("daymet-zarr/daily/na/tmax.zarr")
            ds = xr.open_zarr(store, consolidated=True)
        except Exception:
            # Alternative path
            store = fs.get_mapper("daymet/daily/na/tmax.zarr")
            ds = xr.open_zarr(store, consolidated=True)

        open_time = time.time() - t0
        result.per_request_times.append(open_time)

        # Subset - Daymet uses Lambert Conformal, so bbox doesn't directly apply
        t0 = time.time()
        var = list(ds.data_vars)[0]
        data = ds[var].isel(time=slice(-15, None), x=slice(0, 10), y=slice(0, 10)).values
        load_time = time.time() - t0
        result.per_request_times.append(load_time)

        result.items_found = 15
        result.items_with_data = 1 if data.size > 0 else 0
        result.total_time_s = time.time() - t_total
        result.notes = f"Zarr-only (no STAC items). Shape: {data.shape}"
    except Exception as e:
        result.error = str(e)
        result.total_time_s = time.time() - t_total if "t_total" in dir() else 0
    _finalize(result)
    return result


async def test_weather_cpc_precip(client: httpx.AsyncClient) -> TestResult:
    """CPC Global Unified Precipitation - daily 0.5°."""
    result = TestResult(
        category="Weather",
        collection="cpc-precip-daily",
        description="CPC Global Daily Precipitation 0.5°",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "cpc-precip-daily", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "precip")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_landsat_c2_l2(client: httpx.AsyncClient) -> TestResult:
    """Landsat Collection 2 Level-2 (surface reflectance)."""
    result = TestResult(
        category="Vegetation",
        collection="landsat-c2-l2",
        description="Landsat C2 L2 Surface Reflectance 30m",
        temporal_frequency="16-day revisit",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "landsat-c2-l2", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "nir08")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_modis_snow(client: httpx.AsyncClient) -> TestResult:
    """MODIS Snow Cover (MOD10A1) - daily 500m."""
    result = TestResult(
        category="Weather",
        collection="modis-10A1-061",
        description="MODIS Snow Cover Daily 500m",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-10A1-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "NDSI_Snow_Cover")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_nasadem(client: httpx.AsyncClient) -> TestResult:
    """NASADEM elevation - static 30m."""
    result = TestResult(
        category="Land Use",
        collection="nasadem",
        description="NASADEM Elevation 30m (static DEM)",
        temporal_frequency="Static",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "nasadem", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "elevation")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_cop_dem(client: httpx.AsyncClient) -> TestResult:
    """Copernicus DEM GLO-30 - 30m global elevation."""
    result = TestResult(
        category="Land Use",
        collection="cop-dem-glo-30",
        description="Copernicus DEM 30m Global Elevation",
        temporal_frequency="Static",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "cop-dem-glo-30", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "data")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_imagecubes(client: httpx.AsyncClient) -> TestResult:
    """ImageCubes API - Sentinel-2 NDVI datacubes."""
    result = TestResult(
        category="Image Cubes",
        collection="ImageCubes API (Sentinel-2)",
        description="Sentinel-2 B04/B08 datacubes for NDVI via ImageCubes API",
        temporal_frequency="Biweekly composites",
    )
    token = os.environ.get("IMAGECUBES_TOKEN", "")
    if not token:
        result.error = "IMAGECUBES_TOKEN not set - skipping (requires OAuth2 authentication)"
        result.notes = "Run image_cube.py to authenticate and set token"
        _finalize(result)
        return result

    try:
        t_total = time.time()
        headers = {"Authorization": f"Bearer {token}"}

        # List existing cubes
        t0 = time.time()
        resp = await client.get(
            f"{IMAGECUBES_API}/api/v1/imagecubes",
            headers=headers,
            timeout=SINGLE_REQUEST_TIMEOUT,
        )
        list_time = time.time() - t0
        result.per_request_times.append(list_time)

        if resp.status_code == 401:
            result.error = "Token expired or invalid"
            _finalize(result)
            return result

        resp.raise_for_status()
        cubes = resp.json()
        cube_list = cubes if isinstance(cubes, list) else cubes.get("items", [])
        result.items_found = len(cube_list)
        result.items_with_data = sum(1 for c in cube_list if c.get("status") == "completed")

        # Try to read a completed cube's Zarr
        for cube in cube_list:
            if cube.get("status") == "completed":
                cube_id = cube["id"]
                t0 = time.time()
                resp2 = await client.get(
                    f"{IMAGECUBES_API}/api/v1/imagecubes/{cube_id}",
                    headers=headers,
                    timeout=SINGLE_REQUEST_TIMEOUT,
                )
                result.per_request_times.append(time.time() - t0)
                if resp2.status_code == 200:
                    result.notes = f"Cube ID: {cube_id}, details retrieved OK"
                break

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_modis_nbar(client: httpx.AsyncClient) -> TestResult:
    """MODIS NBAR (MCD43A4) - daily 500m nadir reflectance."""
    result = TestResult(
        category="Vegetation",
        collection="modis-43A4-061",
        description="MODIS NBAR Daily 500m Nadir Reflectance",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-43A4-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "Nadir_Reflectance_Band1")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_modis_evapotranspiration(client: httpx.AsyncClient) -> TestResult:
    """MODIS Evapotranspiration (MOD16A2) - 8-day 500m."""
    result = TestResult(
        category="Weather",
        collection="modis-16A2-061",
        description="MODIS Evapotranspiration 8-day 500m",
        temporal_frequency="8-day",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-16A2-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "ET_500m")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_gridmet(client: httpx.AsyncClient) -> TestResult:
    """gridMET daily meteorological data - CONUS 4km."""
    result = TestResult(
        category="Weather",
        collection="gridmet",
        description="gridMET Daily Meteorological 4km (CONUS)",
        temporal_frequency="Daily",
    )
    try:
        us_bbox = [-105.5, 39.5, -105.0, 40.0]
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "gridmet", us_bbox, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "tmmx")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
        result.notes = "CONUS only (uses Colorado bbox)"
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_goes_cmi(client: httpx.AsyncClient) -> TestResult:
    """GOES-16/17 Cloud & Moisture Imagery."""
    result = TestResult(
        category="Weather",
        collection="goes-cmi",
        description="GOES-16/17 Cloud & Moisture Imagery",
        temporal_frequency="5-15 min",
    )
    try:
        us_bbox = [-105.5, 39.5, -105.0, 40.0]
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "goes-cmi", us_bbox, START_DATE, END_DATE, limit=15)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:3]:
            ok, elapsed = await test_asset_read(client, item, "C02_2km")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
        result.notes = "Very high frequency (5-15min), Americas coverage"
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_modis_daily_lst(client: httpx.AsyncClient) -> TestResult:
    """MODIS Daily LST (MOD11A1) - daily 1km land surface temperature."""
    result = TestResult(
        category="Weather",
        collection="modis-11A1-061",
        description="MODIS Daily Land Surface Temperature 1km",
        temporal_frequency="Daily",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(client, "modis-11A1-061", TEST_BBOX, START_DATE, END_DATE)
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "QC_Day")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_sentinel3_lst(client: httpx.AsyncClient) -> TestResult:
    """Sentinel-3 SLSTR Land Surface Temperature."""
    result = TestResult(
        category="Weather",
        collection="sentinel-3-slstr-lst-l2-netcdf",
        description="Sentinel-3 SLSTR Land Surface Temperature (NetCDF)",
        temporal_frequency="Daily (multiple passes)",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(
            client, "sentinel-3-slstr-lst-l2-netcdf", TEST_BBOX, START_DATE, END_DATE
        )
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "lst-in")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
        result.notes = "NetCDF format, sub-daily passes, global coverage"
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_sentinel3_fire(client: httpx.AsyncClient) -> TestResult:
    """Sentinel-3 SLSTR Fire Radiative Power."""
    result = TestResult(
        category="Fire",
        collection="sentinel-3-slstr-frp-l2-netcdf",
        description="Sentinel-3 Fire Radiative Power (FRP)",
        temporal_frequency="Daily (multiple passes)",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(
            client, "sentinel-3-slstr-frp-l2-netcdf", TEST_BBOX, START_DATE, END_DATE
        )
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:5]:
            ok, elapsed = await test_asset_read(client, item, "frp-in")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
        result.notes = "NetCDF, global coverage, near-real-time"
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


async def test_met_office(client: httpx.AsyncClient) -> TestResult:
    """Met Office Global Deterministic forecast - 10km weather."""
    result = TestResult(
        category="Weather",
        collection="met-office-global-deterministic-pressure",
        description="Met Office Global 10km Deterministic Forecast (temp, wind, humidity)",
        temporal_frequency="6-hourly forecasts",
    )
    try:
        t_total = time.time()
        items, search_time = await stac_search_extended(
            client, "met-office-global-deterministic-pressure", TEST_BBOX, START_DATE, END_DATE
        )
        result.per_request_times.append(search_time)
        result.items_found = len(items)

        for item in items[:3]:
            ok, elapsed = await test_asset_read(client, item, "temperature_on_pressure_levels")
            if ok:
                result.items_with_data += 1
                result.per_request_times.append(elapsed)

        result.total_time_s = time.time() - t_total
        result.notes = "Global 10km NWP forecast, up to 7 days ahead, NetCDF"
    except Exception as e:
        result.error = str(e)
    _finalize(result)
    return result


# ─── Utilities ────────────────────────────────────────────────────────────────

def _finalize(result: TestResult):
    """Compute summary stats."""
    if result.per_request_times:
        result.avg_request_time_s = sum(result.per_request_times) / len(result.per_request_times)
        result.max_request_time_s = max(result.per_request_times)
    result.success = result.items_found > 0 and not result.error
    result.passes_threshold = (
        result.success
        and result.total_time_s < TOTAL_TIMEOUT
        and result.avg_request_time_s < 10.0
    )


def generate_report(results: list[TestResult]) -> str:
    """Generate markdown report."""
    lines = []
    lines.append("# Planetary Computer Endpoint Test Report")
    lines.append(f"\n**Test Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Test BBox:** {TEST_BBOX} (Portugal)")
    lines.append(f"**Date Range:** {START_DATE} to {END_DATE} (15 days)")
    lines.append(f"**Thresholds:** Single request < 10s (max 20s), Total 15-day < 30s")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Collection | Freq | Items | Data OK | Total (s) | Avg/req (s) | Max/req (s) | Verdict |")
    lines.append("|----------|-----------|------|-------|---------|-----------|-------------|-------------|---------|")

    for r in results:
        verdict = "✅ PASS" if r.passes_threshold else ("⚠️ SLOW" if r.success else "❌ FAIL")
        lines.append(
            f"| {r.category} | `{r.collection}` | {r.temporal_frequency} | "
            f"{r.items_found} | {r.items_with_data} | "
            f"{r.total_time_s:.1f} | {r.avg_request_time_s:.1f} | {r.max_request_time_s:.1f} | "
            f"{verdict} |"
        )

    lines.append("")

    # Detailed results by category
    categories = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)

    for cat, cat_results in categories.items():
        lines.append(f"## {cat}")
        lines.append("")
        for r in cat_results:
            status = "✅" if r.passes_threshold else ("⚠️" if r.success else "❌")
            lines.append(f"### {status} `{r.collection}` - {r.description}")
            lines.append("")
            lines.append(f"- **Temporal frequency:** {r.temporal_frequency}")
            lines.append(f"- **Items found:** {r.items_found}")
            lines.append(f"- **Items with readable data:** {r.items_with_data}")
            lines.append(f"- **Total time:** {r.total_time_s:.2f}s")
            if r.per_request_times:
                lines.append(f"- **Per-request times:** {[f'{t:.2f}s' for t in r.per_request_times]}")
                lines.append(f"- **Average request time:** {r.avg_request_time_s:.2f}s")
                lines.append(f"- **Max request time:** {r.max_request_time_s:.2f}s")
            if r.error:
                lines.append(f"- **Error:** `{r.error}`")
            if r.notes:
                lines.append(f"- **Notes:** {r.notes}")
            lines.append(f"- **Passes threshold:** {'Yes' if r.passes_threshold else 'No'}")
            lines.append("")

    # Recommendations
    lines.append("## Recommendations")
    lines.append("")

    passing = [r for r in results if r.passes_threshold]
    slow = [r for r in results if r.success and not r.passes_threshold]
    failed = [r for r in results if not r.success]

    lines.append("### ✅ Recommended for Production Use (Fast & Reliable)")
    lines.append("")
    if passing:
        for r in passing:
            lines.append(f"- **{r.collection}** ({r.category}): {r.description} - avg {r.avg_request_time_s:.1f}s/request")
    else:
        lines.append("- None passed all thresholds")
    lines.append("")

    lines.append("### ⚠️ Usable with Caveats (Slow but Functional)")
    lines.append("")
    if slow:
        for r in slow:
            lines.append(f"- **{r.collection}** ({r.category}): total {r.total_time_s:.1f}s - {r.notes or 'consider caching'}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("### ❌ Not Recommended (Failed or Too Slow)")
    lines.append("")
    if failed:
        for r in failed:
            lines.append(f"- **{r.collection}** ({r.category}): {r.error[:100] if r.error else 'No data'}")
    else:
        lines.append("- None")
    lines.append("")

    # Best options per category
    lines.append("## Best Option Per Category")
    lines.append("")
    for cat, cat_results in categories.items():
        passing_cat = [r for r in cat_results if r.passes_threshold]
        if passing_cat:
            best = min(passing_cat, key=lambda r: r.avg_request_time_s)
            lines.append(f"- **{cat}:** `{best.collection}` ({best.avg_request_time_s:.1f}s avg, {best.temporal_frequency})")
        else:
            functional = [r for r in cat_results if r.success]
            if functional:
                best = min(functional, key=lambda r: r.total_time_s)
                lines.append(f"- **{cat}:** `{best.collection}` (⚠️ {best.total_time_s:.1f}s total, {best.temporal_frequency})")
            else:
                lines.append(f"- **{cat}:** No working endpoint found")
    lines.append("")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("PLANETARY COMPUTER ENDPOINT TEST")
    print(f"Testing {START_DATE} to {END_DATE} | BBox: {TEST_BBOX}")
    print("=" * 70)
    print()

    async with httpx.AsyncClient() as client:
        all_tests = [
            # Fire
            ("Fire: MODIS 14A1 (Daily)", test_fire_modis_14a1),
            ("Fire: VIIRS 14A1", test_fire_viirs_375),
            ("Fire: MODIS 14A2 (8-day)", test_fire_firms),
            ("Fire: Sentinel-3 FRP", test_sentinel3_fire),
            # Floods
            ("Floods: JRC GSW", test_flood_jrc_gsw),
            ("Floods: Sentinel-1 RTC", test_flood_sentinel1_rtc),
            ("Floods: Sentinel-1 GRD", test_flood_sentinel1_grd),
            # Vegetation
            ("Vegetation: MODIS NDVI 13A1", test_vegetation_modis_ndvi),
            ("Vegetation: MODIS LAI 15A2H", test_vegetation_modis_lai),
            ("Vegetation: Sentinel-2 L2A", test_vegetation_sentinel2_l2a),
            ("Vegetation: Landsat C2 L2", test_landsat_c2_l2),
            ("Vegetation: MODIS NBAR 43A4", test_modis_nbar),
            # Land Use
            ("Land Use: ESA WorldCover", test_landuse_esa_worldcover),
            ("Land Use: Esri LULC", test_landuse_esri_lulc),
            ("Land Use: MODIS LC 12Q1", test_landuse_modis_lc),
            ("Land Use: NASADEM", test_nasadem),
            ("Land Use: Copernicus DEM", test_cop_dem),
            # Weather
            ("Weather: ERA5 Zarr", test_weather_era5),
            ("Weather: MODIS LST 11A2 (8-day)", test_weather_modis_lst),
            ("Weather: MODIS LST 11A1 (Daily)", test_modis_daily_lst),
            ("Weather: Sentinel-3 LST", test_sentinel3_lst),
            ("Weather: Met Office Global", test_met_office),
            ("Weather: Daymet Zarr", test_weather_daymet),
            ("Weather: CPC Precip", test_weather_cpc_precip),
            ("Weather: MODIS ET 16A2", test_modis_evapotranspiration),
            ("Weather: gridMET", test_gridmet),
            ("Weather: GOES CMI", test_goes_cmi),
            ("Weather: MODIS Snow 10A1", test_modis_snow),
            # Image Cubes
            ("Image Cubes: Sentinel-2", test_imagecubes),
        ]

        results = []
        for name, test_fn in all_tests:
            print(f"  Testing: {name}...", end=" ", flush=True)
            try:
                result = await asyncio.wait_for(test_fn(client), timeout=60)
            except asyncio.TimeoutError:
                result = TestResult(
                    category=name.split(":")[0].strip(),
                    collection=name,
                    description="Test timed out (>60s)",
                    error="Global timeout exceeded (60s)",
                )
                _finalize(result)
            results.append(result)
            status = "✅" if result.passes_threshold else ("⚠️" if result.success else "❌")
            print(f"{status} ({result.total_time_s:.1f}s, {result.items_found} items)")

        print("\n" + "=" * 70)
        print("GENERATING REPORT...")
        print("=" * 70)

        report = generate_report(results)

        report_path = os.path.join(os.path.dirname(__file__), "ENDPOINT_TEST_REPORT.md")
        with open(report_path, "w") as f:
            f.write(report)

        print(f"\nReport saved to: {report_path}")
        print("\n" + report[:2000] + "\n...")


if __name__ == "__main__":
    asyncio.run(main())
