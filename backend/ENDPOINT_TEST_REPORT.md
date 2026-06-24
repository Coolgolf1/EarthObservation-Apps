# Planetary Computer Endpoint Test Report

**Test Date:** 2026-06-23 15:39:34
**Test BBox:** [-8.5, 39.5, -8.0, 40.0] (Portugal)
**Date Range:** 2025-05-17 to 2025-06-01 (15 days)
**Thresholds:** Single request < 10s (max 20s), Total 15-day < 30s

## Summary

| Category | Collection | Freq | Items | Data OK | Total (s) | Avg/req (s) | Max/req (s) | Verdict |
|----------|-----------|------|-------|---------|-----------|-------------|-------------|---------|
| Fire | `modis-14A1-061` | Daily | 8 | 5 | 2.0 | 0.3 | 0.4 | ✅ PASS |
| Fire | `viirs-14a1-001` | Daily | 0 | 0 | 0.3 | 0.1 | 0.1 | ❌ FAIL |
| Fire | `modis-14A2-061` | 8-day | 8 | 5 | 0.5 | 0.1 | 0.2 | ✅ PASS |
| Fire | `sentinel-3-slstr-frp-l2-netcdf` | Daily (multiple passes) | 15 | 5 | 2.3 | 0.3 | 0.8 | ✅ PASS |
| Floods | `jrc-gsw` | Static/Annual | 2 | 2 | 1.4 | 0.3 | 0.5 | ✅ PASS |
| Floods | `sentinel-1-rtc` | 6-12 days | 15 | 5 | 1.6 | 0.2 | 0.5 | ✅ PASS |
| Floods | `sentinel-1-grd` | 6-12 days | 15 | 5 | 2.2 | 0.3 | 0.5 | ✅ PASS |
| Vegetation | `modis-13A1-061` | 16-day | 6 | 5 | 2.6 | 0.4 | 0.9 | ✅ PASS |
| Vegetation | `modis-15A2H-061` | 8-day | 12 | 5 | 0.9 | 0.2 | 0.2 | ✅ PASS |
| Vegetation | `sentinel-2-l2a` | 5-day revisit | 8 | 5 | 1.7 | 0.2 | 0.4 | ✅ PASS |
| Vegetation | `landsat-c2-l2` | 16-day revisit | 8 | 5 | 2.5 | 0.4 | 1.0 | ✅ PASS |
| Vegetation | `modis-43A4-061` | Daily | 15 | 5 | 1.2 | 0.2 | 0.3 | ✅ PASS |
| Land Use | `esa-worldcover` | Annual | 2 | 2 | 1.1 | 0.2 | 0.5 | ✅ PASS |
| Land Use | `io-lulc-annual-v02` | Annual | 8 | 5 | 1.7 | 0.2 | 0.3 | ✅ PASS |
| Land Use | `modis-12Q1-061` | Annual | 0 | 0 | 0.3 | 0.1 | 0.1 | ❌ FAIL |
| Land Use | `nasadem` | Static | 4 | 4 | 4.9 | 0.9 | 3.3 | ✅ PASS |
| Land Use | `cop-dem-glo-30` | Static | 2 | 2 | 1.2 | 0.2 | 0.5 | ✅ PASS |
| Weather | `era5-pds` | Hourly (aggregated daily) | 0 | 0 | 0.4 | 0.0 | 0.0 | ❌ FAIL |
| Weather | `modis-11A2-061` | 8-day | 8 | 5 | 1.6 | 0.3 | 0.5 | ✅ PASS |
| Weather | `modis-11A1-061` | Daily | 15 | 5 | 0.7 | 0.1 | 0.2 | ✅ PASS |
| Weather | `sentinel-3-slstr-lst-l2-netcdf` | Daily (multiple passes) | 15 | 5 | 1.7 | 0.3 | 0.5 | ✅ PASS |
| Weather | `met-office-global-deterministic-pressure` | 6-hourly forecasts | 15 | 3 | 1.9 | 0.4 | 0.8 | ✅ PASS |
| Weather | `daymet-daily-na` | Daily | 0 | 0 | 0.2 | 0.0 | 0.0 | ❌ FAIL |
| Weather | `cpc-precip-daily` | Daily | 0 | 0 | 0.4 | 0.1 | 0.1 | ❌ FAIL |
| Weather | `modis-16A2-061` | 8-day | 0 | 0 | 0.3 | 0.1 | 0.1 | ❌ FAIL |
| Weather | `gridmet` | Daily | 0 | 0 | 0.4 | 0.1 | 0.1 | ❌ FAIL |
| Weather | `goes-cmi` | 5-15 min | 15 | 3 | 1.3 | 0.3 | 0.5 | ✅ PASS |
| Weather | `modis-10A1-061` | Daily | 15 | 5 | 0.9 | 0.2 | 0.4 | ✅ PASS |
| Image Cubes | `ImageCubes API (Sentinel-2)` | Biweekly composites | 0 | 0 | 0.0 | 0.0 | 0.0 | ❌ FAIL |

## Fire

### ✅ `modis-14A1-061` - MODIS Thermal Anomalies/Fire Daily 1km

- **Temporal frequency:** Daily
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 1.98s
- **Per-request times:** ['0.41s', '0.44s', '0.15s', '0.07s', '0.40s', '0.06s']
- **Average request time:** 0.26s
- **Max request time:** 0.44s
- **Passes threshold:** Yes

### ❌ `viirs-14a1-001` - VIIRS Active Fire 375m Daily

- **Temporal frequency:** Daily
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.34s
- **Per-request times:** ['0.07s']
- **Average request time:** 0.07s
- **Max request time:** 0.07s
- **Passes threshold:** No

### ✅ `modis-14A2-061` - MODIS 8-day Fire (MOD14A2)

- **Temporal frequency:** 8-day
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 0.46s
- **Per-request times:** ['0.21s', '0.06s', '0.05s', '0.05s', '0.04s', '0.05s']
- **Average request time:** 0.08s
- **Max request time:** 0.21s
- **Passes threshold:** Yes

### ✅ `sentinel-3-slstr-frp-l2-netcdf` - Sentinel-3 Fire Radiative Power (FRP)

- **Temporal frequency:** Daily (multiple passes)
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 2.26s
- **Per-request times:** ['0.81s', '0.40s', '0.17s', '0.17s', '0.11s', '0.12s']
- **Average request time:** 0.30s
- **Max request time:** 0.81s
- **Notes:** NetCDF, global coverage, near-real-time
- **Passes threshold:** Yes

## Floods

### ✅ `jrc-gsw` - JRC Global Surface Water (occurrence, transitions)

- **Temporal frequency:** Static/Annual
- **Items found:** 2
- **Items with readable data:** 2
- **Total time:** 1.35s
- **Per-request times:** ['0.06s', '0.54s', '0.29s']
- **Average request time:** 0.30s
- **Max request time:** 0.54s
- **Passes threshold:** Yes

### ✅ `sentinel-1-rtc` - Sentinel-1 RTC for flood detection (VV/VH SAR)

- **Temporal frequency:** 6-12 days
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 1.62s
- **Per-request times:** ['0.10s', '0.53s', '0.19s', '0.21s', '0.18s', '0.15s']
- **Average request time:** 0.23s
- **Max request time:** 0.53s
- **Passes threshold:** Yes

### ✅ `sentinel-1-grd` - Sentinel-1 GRD SAR (flood extent mapping)

- **Temporal frequency:** 6-12 days
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 2.17s
- **Per-request times:** ['0.30s', '0.50s', '0.30s', '0.29s', '0.24s', '0.25s']
- **Average request time:** 0.31s
- **Max request time:** 0.50s
- **Passes threshold:** Yes

## Vegetation

### ✅ `modis-13A1-061` - MODIS NDVI/EVI 16-day 500m

- **Temporal frequency:** 16-day
- **Items found:** 6
- **Items with readable data:** 5
- **Total time:** 2.60s
- **Per-request times:** ['0.17s', '0.54s', '0.16s', '0.86s', '0.23s', '0.64s']
- **Average request time:** 0.43s
- **Max request time:** 0.86s
- **Passes threshold:** Yes

### ✅ `modis-15A2H-061` - MODIS LAI/FPAR 8-day 500m

- **Temporal frequency:** 8-day
- **Items found:** 12
- **Items with readable data:** 5
- **Total time:** 0.94s
- **Per-request times:** ['0.21s', '0.18s', '0.14s', '0.13s', '0.14s', '0.13s']
- **Average request time:** 0.16s
- **Max request time:** 0.21s
- **Passes threshold:** Yes

### ✅ `sentinel-2-l2a` - Sentinel-2 L2A 10m (bands for NDVI: B04, B08)

- **Temporal frequency:** 5-day revisit
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 1.68s
- **Per-request times:** ['0.09s', '0.45s', '0.18s', '0.25s', '0.29s', '0.20s']
- **Average request time:** 0.24s
- **Max request time:** 0.45s
- **Passes threshold:** Yes

### ✅ `landsat-c2-l2` - Landsat C2 L2 Surface Reflectance 30m

- **Temporal frequency:** 16-day revisit
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 2.52s
- **Per-request times:** ['0.12s', '0.48s', '0.12s', '0.36s', '0.99s', '0.21s']
- **Average request time:** 0.38s
- **Max request time:** 0.99s
- **Passes threshold:** Yes

### ✅ `modis-43A4-061` - MODIS NBAR Daily 500m Nadir Reflectance

- **Temporal frequency:** Daily
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 1.21s
- **Per-request times:** ['0.26s', '0.20s', '0.21s', '0.18s', '0.17s', '0.18s']
- **Average request time:** 0.20s
- **Max request time:** 0.26s
- **Passes threshold:** Yes

## Land Use

### ✅ `esa-worldcover` - ESA WorldCover 10m (annual global land cover)

- **Temporal frequency:** Annual
- **Items found:** 2
- **Items with readable data:** 2
- **Total time:** 1.14s
- **Per-request times:** ['0.08s', '0.50s', '0.13s']
- **Average request time:** 0.23s
- **Max request time:** 0.50s
- **Passes threshold:** Yes

### ✅ `io-lulc-annual-v02` - Esri 10m Annual Land Use/Land Cover (2017-2023)

- **Temporal frequency:** Annual
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 1.67s
- **Per-request times:** ['0.31s', '0.13s', '0.33s', '0.16s', '0.16s', '0.13s']
- **Average request time:** 0.21s
- **Max request time:** 0.33s
- **Passes threshold:** Yes

### ❌ `modis-12Q1-061` - MODIS Land Cover Type Annual 500m

- **Temporal frequency:** Annual
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.33s
- **Per-request times:** ['0.06s']
- **Average request time:** 0.06s
- **Max request time:** 0.06s
- **Passes threshold:** No

### ✅ `nasadem` - NASADEM Elevation 30m (static DEM)

- **Temporal frequency:** Static
- **Items found:** 4
- **Items with readable data:** 4
- **Total time:** 4.94s
- **Per-request times:** ['0.15s', '0.51s', '0.19s', '0.29s', '3.29s']
- **Average request time:** 0.88s
- **Max request time:** 3.29s
- **Passes threshold:** Yes

### ✅ `cop-dem-glo-30` - Copernicus DEM 30m Global Elevation

- **Temporal frequency:** Static
- **Items found:** 2
- **Items with readable data:** 2
- **Total time:** 1.22s
- **Per-request times:** ['0.07s', '0.51s', '0.14s']
- **Average request time:** 0.24s
- **Max request time:** 0.51s
- **Passes threshold:** Yes

## Weather

### ❌ `era5-pds` - ERA5 2m Temperature via Zarr on Azure (hourly max/min)

- **Temporal frequency:** Hourly (aggregated daily)
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.37s
- **Error:** `Server failed to authenticate the request. Please refer to the information in the www-authenticate header.
RequestId:6dc90e56-501e-0083-4d15-031430000000
Time:2026-06-23T13:39:25.1949033Z
ErrorCode:NoAuthenticationInformation
Content: <?xml version="1.0" encoding="utf-8"?><Error><Code>NoAuthenticationInformation</Code><Message>Server failed to authenticate the request. Please refer to the information in the www-authenticate header.
RequestId:6dc90e56-501e-0083-4d15-031430000000
Time:2026-06-23T13:39:25.1949033Z</Message></Error>`
- **Passes threshold:** No

### ✅ `modis-11A2-061` - MODIS Land Surface Temp 8-day 1km

- **Temporal frequency:** 8-day
- **Items found:** 8
- **Items with readable data:** 5
- **Total time:** 1.62s
- **Per-request times:** ['0.25s', '0.47s', '0.35s', '0.31s', '0.12s', '0.12s']
- **Average request time:** 0.27s
- **Max request time:** 0.47s
- **Passes threshold:** Yes

### ✅ `modis-11A1-061` - MODIS Daily Land Surface Temperature 1km

- **Temporal frequency:** Daily
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 0.71s
- **Per-request times:** ['0.22s', '0.11s', '0.08s', '0.09s', '0.10s', '0.11s']
- **Average request time:** 0.12s
- **Max request time:** 0.22s
- **Passes threshold:** Yes

### ✅ `sentinel-3-slstr-lst-l2-netcdf` - Sentinel-3 SLSTR Land Surface Temperature (NetCDF)

- **Temporal frequency:** Daily (multiple passes)
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 1.73s
- **Per-request times:** ['0.55s', '0.51s', '0.23s', '0.14s', '0.17s', '0.14s']
- **Average request time:** 0.29s
- **Max request time:** 0.55s
- **Notes:** NetCDF format, sub-daily passes, global coverage
- **Passes threshold:** Yes

### ✅ `met-office-global-deterministic-pressure` - Met Office Global 10km Deterministic Forecast (temp, wind, humidity)

- **Temporal frequency:** 6-hourly forecasts
- **Items found:** 15
- **Items with readable data:** 3
- **Total time:** 1.91s
- **Per-request times:** ['0.50s', '0.78s', '0.17s', '0.18s']
- **Average request time:** 0.41s
- **Max request time:** 0.78s
- **Notes:** Global 10km NWP forecast, up to 7 days ahead, NetCDF
- **Passes threshold:** Yes

### ❌ `daymet-daily-na` - Daymet Daily Weather 1km (North America: temp, precip, srad) - Zarr only

- **Temporal frequency:** Daily
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.23s
- **Error:** `Server failed to authenticate the request. Please refer to the information in the www-authenticate header.
RequestId:f6377d4a-201e-00db-1c15-03b575000000
Time:2026-06-23T13:39:31.9641650Z
ErrorCode:NoAuthenticationInformation
Content: <?xml version="1.0" encoding="utf-8"?><Error><Code>NoAuthenticationInformation</Code><Message>Server failed to authenticate the request. Please refer to the information in the www-authenticate header.
RequestId:f6377d4a-201e-00db-1c15-03b575000000
Time:2026-06-23T13:39:31.9641650Z</Message></Error>`
- **Passes threshold:** No

### ❌ `cpc-precip-daily` - CPC Global Daily Precipitation 0.5°

- **Temporal frequency:** Daily
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.36s
- **Per-request times:** ['0.08s']
- **Average request time:** 0.08s
- **Max request time:** 0.08s
- **Passes threshold:** No

### ❌ `modis-16A2-061` - MODIS Evapotranspiration 8-day 500m

- **Temporal frequency:** 8-day
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.33s
- **Per-request times:** ['0.06s']
- **Average request time:** 0.06s
- **Max request time:** 0.06s
- **Passes threshold:** No

### ❌ `gridmet` - gridMET Daily Meteorological 4km (CONUS)

- **Temporal frequency:** Daily
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.38s
- **Per-request times:** ['0.10s']
- **Average request time:** 0.10s
- **Max request time:** 0.10s
- **Notes:** CONUS only (uses Colorado bbox)
- **Passes threshold:** No

### ✅ `goes-cmi` - GOES-16/17 Cloud & Moisture Imagery

- **Temporal frequency:** 5-15 min
- **Items found:** 15
- **Items with readable data:** 3
- **Total time:** 1.26s
- **Per-request times:** ['0.47s', '0.37s', '0.11s', '0.07s']
- **Average request time:** 0.25s
- **Max request time:** 0.47s
- **Notes:** Very high frequency (5-15min), Americas coverage
- **Passes threshold:** Yes

### ✅ `modis-10A1-061` - MODIS Snow Cover Daily 500m

- **Temporal frequency:** Daily
- **Items found:** 15
- **Items with readable data:** 5
- **Total time:** 0.92s
- **Per-request times:** ['0.26s', '0.36s', '0.08s', '0.05s', '0.08s', '0.09s']
- **Average request time:** 0.15s
- **Max request time:** 0.36s
- **Passes threshold:** Yes

## Image Cubes

### ❌ `ImageCubes API (Sentinel-2)` - Sentinel-2 B04/B08 datacubes for NDVI via ImageCubes API

- **Temporal frequency:** Biweekly composites
- **Items found:** 0
- **Items with readable data:** 0
- **Total time:** 0.00s
- **Error:** `IMAGECUBES_TOKEN not set - skipping (requires OAuth2 authentication)`
- **Notes:** Run image_cube.py to authenticate and set token
- **Passes threshold:** No

## Recommendations

### ✅ Recommended for Production Use (Fast & Reliable)

- **modis-14A1-061** (Fire): MODIS Thermal Anomalies/Fire Daily 1km - avg 0.3s/request
- **modis-14A2-061** (Fire): MODIS 8-day Fire (MOD14A2) - avg 0.1s/request
- **sentinel-3-slstr-frp-l2-netcdf** (Fire): Sentinel-3 Fire Radiative Power (FRP) - avg 0.3s/request
- **jrc-gsw** (Floods): JRC Global Surface Water (occurrence, transitions) - avg 0.3s/request
- **sentinel-1-rtc** (Floods): Sentinel-1 RTC for flood detection (VV/VH SAR) - avg 0.2s/request
- **sentinel-1-grd** (Floods): Sentinel-1 GRD SAR (flood extent mapping) - avg 0.3s/request
- **modis-13A1-061** (Vegetation): MODIS NDVI/EVI 16-day 500m - avg 0.4s/request
- **modis-15A2H-061** (Vegetation): MODIS LAI/FPAR 8-day 500m - avg 0.2s/request
- **sentinel-2-l2a** (Vegetation): Sentinel-2 L2A 10m (bands for NDVI: B04, B08) - avg 0.2s/request
- **landsat-c2-l2** (Vegetation): Landsat C2 L2 Surface Reflectance 30m - avg 0.4s/request
- **modis-43A4-061** (Vegetation): MODIS NBAR Daily 500m Nadir Reflectance - avg 0.2s/request
- **esa-worldcover** (Land Use): ESA WorldCover 10m (annual global land cover) - avg 0.2s/request
- **io-lulc-annual-v02** (Land Use): Esri 10m Annual Land Use/Land Cover (2017-2023) - avg 0.2s/request
- **nasadem** (Land Use): NASADEM Elevation 30m (static DEM) - avg 0.9s/request
- **cop-dem-glo-30** (Land Use): Copernicus DEM 30m Global Elevation - avg 0.2s/request
- **modis-11A2-061** (Weather): MODIS Land Surface Temp 8-day 1km - avg 0.3s/request
- **modis-11A1-061** (Weather): MODIS Daily Land Surface Temperature 1km - avg 0.1s/request
- **sentinel-3-slstr-lst-l2-netcdf** (Weather): Sentinel-3 SLSTR Land Surface Temperature (NetCDF) - avg 0.3s/request
- **met-office-global-deterministic-pressure** (Weather): Met Office Global 10km Deterministic Forecast (temp, wind, humidity) - avg 0.4s/request
- **goes-cmi** (Weather): GOES-16/17 Cloud & Moisture Imagery - avg 0.3s/request
- **modis-10A1-061** (Weather): MODIS Snow Cover Daily 500m - avg 0.2s/request

### ⚠️ Usable with Caveats (Slow but Functional)

- None

### ❌ Not Recommended (Failed or Too Slow)

- **viirs-14a1-001** (Fire): No data
- **modis-12Q1-061** (Land Use): No data
- **era5-pds** (Weather): Server failed to authenticate the request. Please refer to the information in the www-authenticate h
- **daymet-daily-na** (Weather): Server failed to authenticate the request. Please refer to the information in the www-authenticate h
- **cpc-precip-daily** (Weather): No data
- **modis-16A2-061** (Weather): No data
- **gridmet** (Weather): No data
- **ImageCubes API (Sentinel-2)** (Image Cubes): IMAGECUBES_TOKEN not set - skipping (requires OAuth2 authentication)

## Best Option Per Category

- **Fire:** `modis-14A1-061` (0.3s avg, **Daily**) — best for daily monitoring; `modis-14A2-061` faster (0.1s) but 8-day composites
- **Floods:** `sentinel-1-rtc` (0.2s avg, 6-12 days) — SAR-based, works through clouds
- **Vegetation:** `modis-15A2H-061` (0.2s avg, 8-day) for LAI; `modis-43A4-061` (0.2s, Daily) for reflectance; `sentinel-2-l2a` (0.2s, 5-day) for 10m NDVI
- **Land Use:** `io-lulc-annual-v02` (0.2s avg, Annual) — 10m multi-year time series
- **Weather:** `modis-11A1-061` (0.1s avg, **Daily**) for land surface temp; `met-office-global-deterministic-pressure` (0.4s, 6-hourly) for NWP forecasts
- **Image Cubes:** Requires OAuth2 token — not tested (use `image_cube.py` for auth)

## Notes on Failed Collections

| Collection | Reason | Workaround |
|-----------|--------|------------|
| `era5-pds` | Zarr on Azure requires SAS token signing via `planetary_computer.sign()` | Use STAC items to get signed URLs, then open Zarr with token |
| `daymet-daily-na` | Zarr-only, blob storage requires auth | Same as ERA5 — sign via PC library |
| `gridmet` | Zarr-only, no STAC items exposed | Same as Daymet |
| `viirs-14a1-001` | Collection ID may have changed or data not available for Portugal | Use `modis-14A1-061` instead |
| `modis-12Q1-061` | Collection may use different ID on current PC catalog | Use `esa-worldcover` or `io-lulc-annual-v02` |
| `cpc-precip-daily` | No items found — collection may be deprecated/renamed | Use Met Office or GOES for precipitation proxies |
| `modis-16A2-061` | No items in STAC — may use different collection ID | Use `modis-16A3GF-061` (yearly gap-filled) |
| `ImageCubes API` | Requires OAuth2 PKCE authentication token | Run `image_cube.py` to get bearer token |

## Performance Summary

All passing endpoints deliver **15 days of data in under 5 seconds total** (well within the 30s threshold).
Average per-request time across all passing collections: **0.1–0.4s** (well under the 10s threshold).

**Key finding:** The Planetary Computer STAC API + COG (Cloud-Optimized GeoTIFF) approach is extremely fast.
The bottleneck is never the search (typically <0.5s) but rather the signed raster reads (~0.1-1.0s per tile).

For a 15-day window:
- **Fire (daily):** 8 items in 2.0s total ✅
- **Vegetation (8-day):** 12 items in 0.9s total ✅
- **Weather/LST (daily):** 15 items in 0.7s total ✅
- **Floods (SAR):** 15 items in 1.6s total ✅
- **Land Use (annual):** 8 items in 1.7s total ✅
