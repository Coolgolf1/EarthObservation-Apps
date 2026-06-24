# Planetary Computer Rendering Performance Report

**Test Date:** 2026-06-23 15:51:07
**Test BBox:** [-8.5, 39.5, -8.0, 40.0] (Portugal, ~0.5° × 0.5°)
**Date Range:** 2025-05-17 to 2025-06-01 (15 days)
**Thresholds:** Render < 5s (max 10s), Total pipeline < 30s

## H3 vs Raster Rendering Comparison

| Dataset | Pixels | H3 Time | H3 Hexagons | GeoJSON KB | Raster Time | PNG KB | Recommended |
|---------|--------|---------|-------------|-----------|-------------|--------|-------------|
| `fire` | 3,120 | ✅ 0.00s | 0 | 0 | ✅ 0.040s | 1 | **H3** |
| `fire_s3` | - | - | - | - | - | - | ⏭️ NetCDF (skip) |
| `flood_sar` | 4,996,752 | ✅ 0.00s | 0 | 0 | ✅ 0.573s | 435 | **H3** |
| `flood_jrc` | - | - | - | - | - | - | ❌ Failed to load raster data (em |
| `vegetation_ndvi` | 12,360 | ✅ 0.03s | 512 | 262 | ✅ 0.024s | 39 | **H3** |
| `vegetation_lai` | 12,360 | ✅ 0.04s | 512 | 262 | ✅ 0.028s | 22 | **H3** |
| `vegetation_s2` | 4,996,575 | ✅ 0.70s | 1 | 1 | ✅ 0.459s | 450 | **H3** |
| `vegetation_nbar` | 12,360 | ✅ 0.04s | 512 | 257 | ✅ 0.031s | 37 | **H3** |
| `landuse_esa` | 4,999,696 | ✅ 0.72s | 474 | 262 | ✅ 0.204s | 69 | **RASTER** |
| `landuse_esri` | 207,840 | ✅ 0.13s | 1 | 1 | ✅ 0.019s | 9 | **RASTER** |
| `weather_lst_daily` | 3,120 | ✅ 0.01s | 507 | 251 | ✅ 0.016s | 2 | **H3** |
| `weather_lst_8day` | 3,120 | ✅ 0.00s | 507 | 254 | ✅ 0.016s | 15 | **H3** |
| `weather_met_office` | - | - | - | - | - | - | ⏭️ NetCDF (skip) |
| `weather_snow` | 12,360 | ✅ 0.02s | 507 | 254 | ✅ 0.013s | 3 | **H3** |
| `dem_copernicus` | 3,238,200 | ✅ 0.69s | 474 | 243 | ✅ 0.259s | 355 | **RASTER** |

## 15-Day Multi-Frame Pipeline Results

| Dataset | Frames | Total (s) | Load (s) | H3 Total (s) | Raster Total (s) | H3 <5s? | Raster <5s? | Pipeline <30s? |
|---------|--------|-----------|----------|--------------|-----------------|---------|-------------|----------------|
| `fire` | 8 | 4.5 | 4.1 | 0.00 | 0.12 | ✅ | ✅ | ✅ |
| `vegetation_ndvi` | 6 | 1.3 | 0.9 | 0.07 | 0.09 | ✅ | ✅ | ✅ |
| `vegetation_lai` | 12 | 3.2 | 2.5 | 0.26 | 0.17 | ✅ | ✅ | ✅ |
| `weather_lst_daily` | 15 | 3.3 | 2.7 | 0.05 | 0.26 | ✅ | ✅ | ✅ |
| `weather_snow` | 15 | 3.1 | 2.7 | 0.07 | 0.16 | ✅ | ✅ | ✅ |
| `landuse_esri` | 8 | 8.1 | 3.0 | 3.13 | 1.40 | ✅ | ✅ | ✅ |

## Analysis & Recommendations

### Rendering Method Verdict

- **H3 recommended for 9 datasets:** `fire`, `flood_sar`, `vegetation_ndvi`, `vegetation_lai`, `vegetation_s2`, `vegetation_nbar`, `weather_lst_daily`, `weather_lst_8day`, `weather_snow`
- **Raster recommended for 3 datasets:** `landuse_esa`, `landuse_esri`, `dem_copernicus`

### Why H3 (Hexagons)?

- **Pros:** Consistent spatial aggregation, interactive filtering by hexagon, multi-dataset overlay alignment, temporal comparison, client-side styling
- **Cons:** Conversion overhead (0.01–2s), larger GeoJSON payload, polygon rendering more complex than image overlay
- **Best for:** Classification data (fire, land use), coarse resolution (MODIS 500m/1km), analytics dashboards, multi-dataset comparison

### Why Raster (Image Overlay)?

- **Pros:** Near-instant rendering (<0.1s), smaller payload (PNG), pixel-perfect precision, natural visualization, trivial caching (tile-based)
- **Cons:** No client-side filtering, re-render needed for colormap changes, alignment harder for multi-dataset overlay
- **Best for:** High-res data (Sentinel-2 10m, Landsat 30m, ESA WorldCover), continuous values (NDVI, temperature), background layers

### Hybrid Approach (Recommended)

Use **both** methods depending on context:

1. **Background layer:** Raster overlay (fast, pretty, cached as tiles)
2. **Interactive layer:** H3 hexagons (clickable, filterable, animatable)
3. **Multi-day animation:** Pre-render raster tiles per frame, overlay H3 for stats

```
Single-day view:  Raster tile (0.01–0.1s) + H3 overlay (0.01–2s) = instant
15-day animation: Pre-cached raster tiles + H3 aggregates per frame
```

### Best Collection Per Category (with Rendering Method)

- **Fire:** `fire_s3` (Sentinel-3 Fire Radiative Power) — render via **** (0.00s H3 / 0.000s raster, 0 hexagons, Daily)
- **Floods:** `flood_sar` (Sentinel-1 RTC SAR (flood detection)) — render via **H3** (0.00s H3 / 0.573s raster, 0 hexagons, 6-12 days)
- **Vegetation:** `vegetation_ndvi` (MODIS NDVI 16-day 500m) — render via **H3** (0.03s H3 / 0.024s raster, 512 hexagons, 16-day)
- **Land Use:** `landuse_esri` (Esri 10m LULC Annual) — render via **RASTER** (0.13s H3 / 0.019s raster, 1 hexagons, Annual)
- **Weather:** `weather_met_office` (Met Office Global 10km Forecast) — render via **** (0.00s H3 / 0.000s raster, 0 hexagons, 6-hourly)
- **Elevation:** `dem_copernicus` (Copernicus DEM 30m) — render via **RASTER** (0.69s H3 / 0.259s raster, 474 hexagons, Static)

## Detailed Per-Dataset Notes

### ✅ `fire` — MODIS Daily Fire 1km

- **Load time:** 1.38s (3,120 pixels, shape (60, 52))
- **H3 conversion:** 0.000s → 0 hexagons (res 7)
- **H3 GeoJSON payload:** 0 KB
- **Raster render:** 0.040s → 1 KB PNG
- **Total pipeline:** 1.42s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ❌ `fire_s3` — Sentinel-3 Fire Radiative Power

- **Load time:** 0.29s (0 pixels, shape ())
- **H3 conversion:** 0.000s → 0 hexagons (res 7)
- **H3 GeoJSON payload:** 0 KB
- **Raster render:** 0.000s → 0 KB PNG
- **Total pipeline:** 0.00s
- **Recommended:** 
- **Notes:** NetCDF format — STAC search OK but COG raster test skipped | 15 items available

### ✅ `flood_sar` — Sentinel-1 RTC SAR (flood detection)

- **Load time:** 7.58s (4,996,752 pixels, shape (2539, 1968))
- **H3 conversion:** 0.002s → 0 hexagons (res 7)
- **H3 GeoJSON payload:** 0 KB
- **Raster render:** 0.573s → 435 KB PNG
- **Total pipeline:** 8.15s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ❌ `flood_jrc` — JRC Global Surface Water

- **Error:** Failed to load raster data (empty window)

### ✅ `vegetation_ndvi` — MODIS NDVI 16-day 500m

- **Load time:** 0.41s (12,360 pixels, shape (120, 103))
- **H3 conversion:** 0.026s → 512 hexagons (res 7)
- **H3 GeoJSON payload:** 262 KB
- **Raster render:** 0.024s → 39 KB PNG
- **Total pipeline:** 0.46s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `vegetation_lai` — MODIS LAI 8-day 500m

- **Load time:** 0.44s (12,360 pixels, shape (120, 103))
- **H3 conversion:** 0.042s → 512 hexagons (res 7)
- **H3 GeoJSON payload:** 262 KB
- **Raster render:** 0.028s → 22 KB PNG
- **Total pipeline:** 0.52s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `vegetation_s2` — Sentinel-2 L2A 10m (NIR for NDVI)

- **Load time:** 1.68s (4,996,575 pixels, shape (2095, 2385))
- **H3 conversion:** 0.699s → 1 hexagons (res 7)
- **H3 GeoJSON payload:** 1 KB
- **Raster render:** 0.459s → 450 KB PNG
- **Total pipeline:** 2.84s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `vegetation_nbar` — MODIS NBAR Daily 500m

- **Load time:** 0.53s (12,360 pixels, shape (120, 103))
- **H3 conversion:** 0.041s → 512 hexagons (res 7)
- **H3 GeoJSON payload:** 257 KB
- **Raster render:** 0.031s → 37 KB PNG
- **Total pipeline:** 0.60s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `landuse_esa` — ESA WorldCover 10m

- **Load time:** 1.60s (4,999,696 pixels, shape (2236, 2236))
- **H3 conversion:** 0.724s → 474 hexagons (res 7)
- **H3 GeoJSON payload:** 262 KB
- **Raster render:** 0.204s → 69 KB PNG
- **Total pipeline:** 2.53s
- **Recommended:** RASTER
- **Notes:** Raster significantly faster; use ImageOverlay for map rendering

### ✅ `landuse_esri` — Esri 10m LULC Annual

- **Load time:** 1.10s (207,840 pixels, shape (48, 4330))
- **H3 conversion:** 0.132s → 1 hexagons (res 7)
- **H3 GeoJSON payload:** 1 KB
- **Raster render:** 0.019s → 9 KB PNG
- **Total pipeline:** 1.25s
- **Recommended:** RASTER
- **Notes:** Raster significantly faster; use ImageOverlay for map rendering

### ✅ `weather_lst_daily` — MODIS Daily LST 1km

- **Load time:** 0.74s (3,120 pixels, shape (60, 52))
- **H3 conversion:** 0.005s → 507 hexagons (res 7)
- **H3 GeoJSON payload:** 251 KB
- **Raster render:** 0.016s → 2 KB PNG
- **Total pipeline:** 0.77s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `weather_lst_8day` — MODIS LST 8-day 1km

- **Load time:** 0.46s (3,120 pixels, shape (60, 52))
- **H3 conversion:** 0.005s → 507 hexagons (res 7)
- **H3 GeoJSON payload:** 254 KB
- **Raster render:** 0.016s → 15 KB PNG
- **Total pipeline:** 0.48s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ❌ `weather_met_office` — Met Office Global 10km Forecast

- **Load time:** 0.38s (0 pixels, shape ())
- **H3 conversion:** 0.000s → 0 hexagons (res 5)
- **H3 GeoJSON payload:** 0 KB
- **Raster render:** 0.000s → 0 KB PNG
- **Total pipeline:** 0.00s
- **Recommended:** 
- **Notes:** NetCDF format — STAC search OK but COG raster test skipped | 15 items available

### ✅ `weather_snow` — MODIS Snow Cover Daily 500m

- **Load time:** 0.42s (12,360 pixels, shape (120, 103))
- **H3 conversion:** 0.019s → 507 hexagons (res 7)
- **H3 GeoJSON payload:** 254 KB
- **Raster render:** 0.013s → 3 KB PNG
- **Total pipeline:** 0.46s
- **Recommended:** H3
- **Notes:** H3 fast with reasonable hexagon count; good for interactive filtering

### ✅ `dem_copernicus` — Copernicus DEM 30m

- **Load time:** 3.28s (3,238,200 pixels, shape (1800, 1799))
- **H3 conversion:** 0.688s → 474 hexagons (res 7)
- **H3 GeoJSON payload:** 243 KB
- **Raster render:** 0.259s → 355 KB PNG
- **Total pipeline:** 4.23s
- **Recommended:** RASTER
- **Notes:** Raster significantly faster; use ImageOverlay for map rendering
