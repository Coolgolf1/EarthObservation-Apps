# Databricks notebook source
# MAGIC %md
# MAGIC # Earth Datasets — Fire, Flood, Weather, Vegetation, Land Use
# MAGIC
# MAGIC Multi-dataset visualization using Planetary Computer STAC + Zarr/COG access.
# MAGIC Each section loads real satellite data and renders it with Plotly.

# COMMAND ----------

import numpy as np
import planetary_computer
import pystac_client
import rasterio
import xarray as xr
import zarr
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from dash import Dash, Input, Output, dcc, html

catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
print("Connected to Planetary Computer STAC API")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Common utilities

# COMMAND ----------

def window_for_bbox(ds, bbox_wgs84):
    """Compute rasterio window for a WGS84 bounding box."""
    west, south, east, north = bbox_wgs84
    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
    )
    candidate = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
    bounded = candidate.intersection(Window(0, 0, ds.width, ds.height))
    if bounded.width <= 0 or bounded.height <= 0:
        return None
    return bounded.round_offsets().round_lengths()


def load_raster_band(href, bbox, band=1):
    """Load a single band from a signed COG href, cropped to bbox."""
    with rasterio.open(href) as ds:
        window = window_for_bbox(ds, bbox)
        if window is None:
            return None
        data = ds.read(band, window=window, masked=True).astype("float32")
        return np.where(data.mask, np.nan, data.data)


def search_items(collection, geometry, datetimes):
    """Search for STAC items across multiple dates."""
    items = {}
    for dt in datetimes:
        search = catalog.search(
            collections=[collection],
            intersects=geometry,
            datetime=dt,
        )
        results = search.item_collection()
        if len(results) > 0:
            items[dt] = results[0]
    return items


# Default study area — Mediterranean (Spain/Portugal fire season 2023)
latitude = 39.5
longitude = -5.5
buffer = 2.0
bbox = [longitude - buffer, latitude - buffer, longitude + buffer, latitude + buffer]
geometry = {"type": "Point", "coordinates": [longitude, latitude]}

print(f"Study area: {bbox}")
print(f"Center: {latitude}°N, {longitude}°E")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 1. 🔥 Fire — MODIS Daily FireMask (modis-14A1-061)
# MAGIC
# MAGIC Reads actual FireMask pixel values (classes 7=low, 8=nominal, 9=high confidence fire).

# COMMAND ----------

# Fire dates — summer 2023 fire season in Iberian Peninsula
fire_dates = [
    "2023-07-10", "2023-07-15", "2023-07-20", "2023-07-25",
    "2023-07-30", "2023-08-04", "2023-08-09", "2023-08-14",
]

fire_items = search_items("modis-14A1-061", geometry, fire_dates)
print(f"Found {len(fire_items)} fire items")

# Load FireMask rasters
fire_arrays = []
fire_timestamps = []

for dt in sorted(fire_items.keys()):
    item = fire_items[dt]
    asset = item.assets.get("FireMask")
    if asset is None:
        continue
    arr = load_raster_band(asset.href, bbox)
    if arr is None:
        continue
    if fire_arrays and arr.shape != fire_arrays[0].shape:
        h = min(arr.shape[0], fire_arrays[0].shape[0])
        w = min(arr.shape[1], fire_arrays[0].shape[1])
        fire_arrays = [a[:h, :w] for a in fire_arrays]
        arr = arr[:h, :w]
    fire_arrays.append(arr)
    fire_timestamps.append(dt)

fire_stack = np.stack(fire_arrays, axis=0) if fire_arrays else np.array([])
print(f"Fire stack shape: {fire_stack.shape} — {len(fire_timestamps)} dates loaded")

# COMMAND ----------

# Plot fire data — only show fire pixels (classes 7-9) like the fire.ipynb
if fire_stack.size > 0:
    fire_only = np.where(fire_stack >= 7, fire_stack, np.nan)

    fig = make_subplots(
        rows=2, cols=4,
        subplot_titles=[f"🔥 {d}" for d in fire_timestamps],
        horizontal_spacing=0.02, vertical_spacing=0.08,
    )

    for i, (arr, dt) in enumerate(zip(fire_only, fire_timestamps)):
        row = i // 4 + 1
        col = i % 4 + 1
        fig.add_trace(
            go.Heatmap(
                z=arr, colorscale="YlOrRd", zmin=7, zmax=9,
                showscale=(i == 0),
                colorbar=dict(title="Fire Class", tickvals=[7, 8, 9], ticktext=["Low", "Nom", "High"]),
            ),
            row=row, col=col,
        )

    fig.update_layout(
        title="MODIS FireMask — Active Fire Detections (pixel values 7-9)",
        height=500, width=1000,
        template="plotly_white",
    )
    fig.update_xaxes(showticklabels=False)
    fig.update_yaxes(showticklabels=False)
    fig.show()
else:
    print("No fire data loaded — try different dates or area")

# COMMAND ----------

# Fire time series — count fire pixels per day by confidence level
if fire_stack.size > 0:
    fire_stats = []
    for i, dt in enumerate(fire_timestamps):
        arr = fire_stack[i]
        fire_stats.append({
            "date": dt,
            "fire_low": int(np.nansum(arr == 7)),
            "fire_nominal": int(np.nansum(arr == 8)),
            "fire_high": int(np.nansum(arr == 9)),
            "total_fire": int(np.nansum((arr >= 7) & (arr <= 9))),
            "cloud": int(np.nansum(arr == 5)),
        })

    import pandas as pd
    df_fire = pd.DataFrame(fire_stats)
    
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df_fire["date"], y=df_fire["fire_high"], name="High confidence", marker_color="#d32f2f"))
    fig.add_trace(go.Bar(x=df_fire["date"], y=df_fire["fire_nominal"], name="Nominal", marker_color="#ff8f00"))
    fig.add_trace(go.Bar(x=df_fire["date"], y=df_fire["fire_low"], name="Low confidence", marker_color="#fdd835"))
    fig.update_layout(
        barmode="stack",
        title="Fire Pixel Count by Confidence Level Over Time",
        xaxis_title="Date", yaxis_title="Number of fire pixels",
        template="plotly_white", height=350,
    )
    fig.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 2. 🌊 Flood — JRC Global Surface Water (jrc-gsw)
# MAGIC
# MAGIC Monthly water occurrence from the JRC Global Surface Water dataset.
# MAGIC Detects changes in water extent that indicate flooding.

# COMMAND ----------

# JRC Global Surface Water — monthly water recurrence
# Use a flood-prone area (Bangladesh/India monsoon region)
flood_lat = 23.5
flood_lon = 90.0
flood_buffer = 1.5
flood_bbox = [flood_lon - flood_buffer, flood_lat - flood_buffer,
              flood_lon + flood_buffer, flood_lat + flood_buffer]
flood_geometry = {"type": "Point", "coordinates": [flood_lon, flood_lat]}

# Search for JRC Global Surface Water monthly data
flood_search = catalog.search(
    collections=["jrc-gsw"],
    intersects=flood_geometry,
    limit=1,
)
flood_results = list(flood_search.item_collection())
print(f"JRC-GSW items found: {len(flood_results)}")

if flood_results:
    flood_item = flood_results[0]
    print(f"Item: {flood_item.id}")
    print(f"Assets: {list(flood_item.assets.keys())}")

# COMMAND ----------

# Load water occurrence and seasonality bands
if flood_results:
    flood_item = flood_results[0]
    
    # Try different asset names the collection might have
    water_asset = None
    for key in ["occurrence", "seasonality", "recurrence", "change"]:
        if key in flood_item.assets:
            water_asset = flood_item.assets[key]
            print(f"Using asset: {key} — {water_asset.title if hasattr(water_asset, 'title') else key}")
            break
    
    if water_asset:
        water_arr = load_raster_band(water_asset.href, flood_bbox)
        if water_arr is not None:
            # Water occurrence: 0-100 (percentage of time water is present)
            fig = px.imshow(
                water_arr,
                color_continuous_scale="Blues",
                zmin=0, zmax=100,
                title=f"JRC Global Surface Water — {key.title()} (Bangladesh region)",
                labels={"color": f"Water {key} (%)"},
                origin="upper",
                aspect="equal",
            )
            fig.update_layout(height=500, width=600, template="plotly_white")
            fig.show()

            # Histogram of water occurrence values
            valid = water_arr[~np.isnan(water_arr)].flatten()
            fig2 = px.histogram(
                x=valid[valid > 0], nbins=50,
                title=f"Distribution of Water {key.title()} Values",
                labels={"x": f"{key.title()} (%)", "y": "Pixel count"},
                color_discrete_sequence=["#1565c0"],
            )
            fig2.update_layout(height=300, template="plotly_white")
            fig2.show()
        else:
            print("Could not load water raster for this bbox")
    else:
        print(f"No recognized water asset. Available: {list(flood_item.assets.keys())}")
else:
    print("No JRC-GSW data found")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 3. 🌿 Vegetation — MODIS NDVI (modis-13A1-061)
# MAGIC
# MAGIC 16-day NDVI composite showing vegetation health. Low NDVI can indicate drought stress.

# COMMAND ----------

# MODIS Vegetation Index — 500m 16-day composite
ndvi_dates = [
    "2023-06-01", "2023-06-17", "2023-07-04", "2023-07-20",
    "2023-08-05", "2023-08-21", "2023-09-06", "2023-09-22",
]

ndvi_items = search_items("modis-13A1-061", geometry, ndvi_dates)
print(f"Found {len(ndvi_items)} NDVI items")

if ndvi_items:
    first_key = next(iter(ndvi_items))
    print(f"Assets available: {list(ndvi_items[first_key].assets.keys())}")

# COMMAND ----------

# Load NDVI band (500m_16_days_NDVI)
ndvi_arrays = []
ndvi_timestamps = []

for dt in sorted(ndvi_items.keys()):
    item = ndvi_items[dt]
    # MODIS 13A1 asset name for NDVI
    asset = item.assets.get("500m_16_days_NDVI") or item.assets.get("NDVI")
    if asset is None:
        # Try first available raster asset
        for k, v in item.assets.items():
            if "ndvi" in k.lower():
                asset = v
                break
    if asset is None:
        continue
    
    arr = load_raster_band(asset.href, bbox)
    if arr is None:
        continue
    
    # MODIS NDVI scale factor: divide by 10000 to get -1 to 1 range
    arr = arr / 10000.0
    arr = np.where((arr >= -1) & (arr <= 1), arr, np.nan)
    
    if ndvi_arrays and arr.shape != ndvi_arrays[0].shape:
        h = min(arr.shape[0], ndvi_arrays[0].shape[0])
        w = min(arr.shape[1], ndvi_arrays[0].shape[1])
        ndvi_arrays = [a[:h, :w] for a in ndvi_arrays]
        arr = arr[:h, :w]
    
    ndvi_arrays.append(arr)
    ndvi_timestamps.append(dt)

if ndvi_arrays:
    ndvi_stack = np.stack(ndvi_arrays, axis=0)
    print(f"NDVI stack shape: {ndvi_stack.shape}")
else:
    ndvi_stack = np.array([])
    print("No NDVI data loaded")

# COMMAND ----------

# Plot NDVI maps — green=healthy vegetation, brown/yellow=stressed
if ndvi_stack.size > 0:
    n = len(ndvi_timestamps)
    cols = min(4, n)
    rows = (n + cols - 1) // cols

    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=[f"🌿 {d}" for d in ndvi_timestamps],
        horizontal_spacing=0.02, vertical_spacing=0.08,
    )

    for i, (arr, dt) in enumerate(zip(ndvi_stack, ndvi_timestamps)):
        row = i // cols + 1
        col = i % cols + 1
        fig.add_trace(
            go.Heatmap(
                z=arr, colorscale="RdYlGn", zmin=-0.2, zmax=0.9,
                showscale=(i == 0),
                colorbar=dict(title="NDVI"),
            ),
            row=row, col=col,
        )

    fig.update_layout(
        title="MODIS NDVI — Vegetation Health (green=healthy, red=stressed/bare)",
        height=300 * rows, width=1000,
        template="plotly_white",
    )
    fig.update_xaxes(showticklabels=False)
    fig.update_yaxes(showticklabels=False)
    fig.show()

# COMMAND ----------

# NDVI time series — mean vegetation health over time
if ndvi_stack.size > 0:
    ndvi_means = [float(np.nanmean(arr)) for arr in ndvi_stack]
    ndvi_stds = [float(np.nanstd(arr)) for arr in ndvi_stack]
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ndvi_timestamps, y=ndvi_means,
        mode="lines+markers",
        name="Mean NDVI",
        line=dict(color="#2e7d32", width=3),
        marker=dict(size=8),
    ))
    fig.add_trace(go.Scatter(
        x=ndvi_timestamps + ndvi_timestamps[::-1],
        y=[m + s for m, s in zip(ndvi_means, ndvi_stds)] +
          [m - s for m, s in zip(ndvi_means[::-1], ndvi_stds[::-1])],
        fill="toself", fillcolor="rgba(46,125,50,0.15)",
        line=dict(color="rgba(0,0,0,0)"),
        name="±1 std dev",
    ))
    fig.update_layout(
        title="Vegetation Health Over Time (Mean NDVI ± σ)",
        xaxis_title="Date", yaxis_title="NDVI",
        template="plotly_white", height=350,
        yaxis=dict(range=[0, 1]),
    )
    fig.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 4. 🌡️ Weather — ERA5 Land Surface Temperature (era5-pds)
# MAGIC
# MAGIC Monthly land surface temperature from Copernicus ERA5 reanalysis (via Zarr).

# COMMAND ----------

# ERA5 monthly data via Zarr (Planetary Computer hosts this as Zarr store)
# Search for available ERA5/climate data
weather_search = catalog.search(
    collections=["era5-pds"],
    bbox=bbox,
    limit=1,
)
weather_results = list(weather_search.item_collection())

if not weather_results:
    # Try CPC global temperature dataset as alternative
    weather_search = catalog.search(
        collections=["cpc-global-temp"],
        bbox=bbox,
        datetime="2023-07-01/2023-08-31",
        limit=5,
    )
    weather_results = list(weather_search.item_collection())

if not weather_results:
    # Use MODIS Land Surface Temperature as weather proxy
    weather_search = catalog.search(
        collections=["modis-11A2-061"],
        intersects=geometry,
        datetime="2023-07-01/2023-08-31",
        limit=10,
    )
    weather_results = list(weather_search.item_collection())

print(f"Weather/temperature items found: {len(weather_results)}")
if weather_results:
    print(f"Collection: {weather_results[0].collection_id}")
    print(f"Assets: {list(weather_results[0].assets.keys())}")

# COMMAND ----------

# Load Land Surface Temperature data
lst_arrays = []
lst_timestamps = []

for item in weather_results[:8]:
    # MODIS LST asset names
    asset = None
    for key in ["LST_Day_1km", "LST_Night_1km", "LST_Day_6km", "rendered_preview"]:
        if key in item.assets:
            asset = item.assets[key]
            break
    if asset is None:
        # Use first non-metadata asset
        for k, v in item.assets.items():
            if k not in ("tilejson", "rendered_preview", "metadata"):
                asset = v
                break
    if asset is None:
        continue
    
    arr = load_raster_band(asset.href, bbox)
    if arr is None:
        continue
    
    # MODIS LST scale: multiply by 0.02 to get Kelvin, then convert to Celsius
    if np.nanmax(arr) > 200:  # likely in Kelvin*50 scale
        arr = arr * 0.02 - 273.15
    
    if lst_arrays and arr.shape != lst_arrays[0].shape:
        h = min(arr.shape[0], lst_arrays[0].shape[0])
        w = min(arr.shape[1], lst_arrays[0].shape[1])
        lst_arrays = [a[:h, :w] for a in lst_arrays]
        arr = arr[:h, :w]
    
    lst_arrays.append(arr)
    dt = item.datetime.strftime("%Y-%m-%d") if item.datetime else item.id
    lst_timestamps.append(dt)

if lst_arrays:
    lst_stack = np.stack(lst_arrays, axis=0)
    print(f"LST stack shape: {lst_stack.shape} — {len(lst_timestamps)} dates")
else:
    lst_stack = np.array([])
    print("No LST data loaded")

# COMMAND ----------

# Plot Land Surface Temperature maps
if lst_stack.size > 0:
    n = len(lst_timestamps)
    cols = min(4, n)
    rows = (n + cols - 1) // cols

    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=[f"🌡️ {d}" for d in lst_timestamps],
        horizontal_spacing=0.02, vertical_spacing=0.08,
    )

    vmin = float(np.nanpercentile(lst_stack, 5))
    vmax = float(np.nanpercentile(lst_stack, 95))

    for i, (arr, dt) in enumerate(zip(lst_stack, lst_timestamps)):
        row = i // cols + 1
        col = i % cols + 1
        fig.add_trace(
            go.Heatmap(
                z=arr, colorscale="Hot", zmin=vmin, zmax=vmax,
                showscale=(i == 0),
                colorbar=dict(title="°C"),
            ),
            row=row, col=col,
        )

    fig.update_layout(
        title="Land Surface Temperature (°C) — MODIS Thermal",
        height=300 * rows, width=1000,
        template="plotly_white",
    )
    fig.update_xaxes(showticklabels=False)
    fig.update_yaxes(showticklabels=False)
    fig.show()

    # Temperature time series
    lst_means = [float(np.nanmean(arr)) for arr in lst_stack]
    lst_maxs = [float(np.nanmax(arr)) for arr in lst_stack]
    
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=lst_timestamps, y=lst_means, mode="lines+markers",
                              name="Mean LST", line=dict(color="#e65100", width=3)))
    fig2.add_trace(go.Scatter(x=lst_timestamps, y=lst_maxs, mode="lines+markers",
                              name="Max LST", line=dict(color="#d32f2f", width=2, dash="dash")))
    fig2.update_layout(
        title="Land Surface Temperature Over Time",
        xaxis_title="Date", yaxis_title="Temperature (°C)",
        template="plotly_white", height=350,
    )
    fig2.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 5. 🏞️ Land Use / Land Cover — ESA WorldCover (esa-worldcover)
# MAGIC
# MAGIC 10m land cover classification showing urban, forest, cropland, water, etc.

# COMMAND ----------

# ESA WorldCover 10m land cover
landcover_search = catalog.search(
    collections=["esa-worldcover"],
    intersects=geometry,
    limit=1,
)
landcover_results = list(landcover_search.item_collection())
print(f"ESA WorldCover items: {len(landcover_results)}")

if landcover_results:
    lc_item = landcover_results[0]
    print(f"Item: {lc_item.id}")
    print(f"Assets: {list(lc_item.assets.keys())}")

# COMMAND ----------

# Load and plot land cover
if landcover_results:
    lc_item = landcover_results[0]
    asset = lc_item.assets.get("map") or next(
        (v for k, v in lc_item.assets.items() if k not in ("tilejson", "rendered_preview")), None
    )
    
    if asset:
        # Use smaller bbox for 10m data (would be huge otherwise)
        lc_bbox = [longitude - 0.3, latitude - 0.3, longitude + 0.3, latitude + 0.3]
        lc_arr = load_raster_band(asset.href, lc_bbox)
        
        if lc_arr is not None:
            # ESA WorldCover classes
            lc_classes = {
                10: "Tree cover",
                20: "Shrubland",
                30: "Grassland",
                40: "Cropland",
                50: "Built-up",
                60: "Bare/sparse",
                70: "Snow/Ice",
                80: "Water",
                90: "Herbaceous wetland",
                95: "Mangroves",
                100: "Moss/Lichen",
            }
            
            # Custom discrete colorscale for land cover
            lc_colors = {
                10: "#006400", 20: "#ffbb22", 30: "#ffff4c", 40: "#f096ff",
                50: "#fa0000", 60: "#b4b4b4", 70: "#f0f0f0", 80: "#0064c8",
                90: "#0096a0", 95: "#00cf75", 100: "#fae6a0",
            }

            fig = px.imshow(
                lc_arr,
                color_continuous_scale=[
                    [0, "#006400"], [0.1, "#006400"],     # Trees
                    [0.1, "#ffbb22"], [0.2, "#ffbb22"],   # Shrub
                    [0.2, "#ffff4c"], [0.3, "#ffff4c"],   # Grass
                    [0.3, "#f096ff"], [0.4, "#f096ff"],   # Crop
                    [0.4, "#fa0000"], [0.5, "#fa0000"],   # Built
                    [0.5, "#b4b4b4"], [0.6, "#b4b4b4"],   # Bare
                    [0.6, "#f0f0f0"], [0.7, "#f0f0f0"],   # Snow
                    [0.7, "#0064c8"], [0.8, "#0064c8"],   # Water
                    [0.8, "#0096a0"], [0.9, "#0096a0"],   # Wetland
                    [0.9, "#00cf75"], [1.0, "#00cf75"],    # Mangroves
                ],
                zmin=10, zmax=100,
                title="ESA WorldCover — Land Use / Land Cover (10m)",
                labels={"color": "Class"},
                origin="upper", aspect="equal",
            )
            fig.update_layout(height=600, width=700, template="plotly_white")
            fig.show()

            # Class distribution pie chart
            valid = lc_arr[~np.isnan(lc_arr)].flatten().astype(int)
            unique, counts = np.unique(valid, return_counts=True)
            labels = [lc_classes.get(u, f"Class {u}") for u in unique]
            colors = [lc_colors.get(u, "#888888") for u in unique]
            
            fig2 = go.Figure(data=[go.Pie(
                labels=labels, values=counts, marker_colors=colors,
                textinfo="label+percent", hole=0.3,
            )])
            fig2.update_layout(
                title="Land Cover Class Distribution",
                height=400, template="plotly_white",
            )
            fig2.show()
        else:
            print("Could not load land cover raster")
    else:
        print("No map asset found")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## 6. 📊 Combined Dashboard — All Datasets
# MAGIC
# MAGIC Interactive Dash app comparing fire, vegetation, and temperature in a single view.

# COMMAND ----------

# Combined analysis — correlate fire with NDVI and temperature
import pandas as pd

combined_data = []

# Build a combined dataframe from all available time series
for i, dt in enumerate(fire_timestamps):
    entry = {"date": dt, "dataset": "fire"}
    arr = fire_stack[i]
    entry["fire_pixels"] = int(np.nansum((arr >= 7) & (arr <= 9)))
    entry["fire_high"] = int(np.nansum(arr == 9))
    combined_data.append(entry)

for i, dt in enumerate(ndvi_timestamps):
    if ndvi_stack.size > 0:
        entry = {"date": dt, "dataset": "ndvi"}
        entry["ndvi_mean"] = float(np.nanmean(ndvi_stack[i]))
        entry["ndvi_min"] = float(np.nanmin(ndvi_stack[i]))
        combined_data.append(entry)

for i, dt in enumerate(lst_timestamps):
    if lst_stack.size > 0:
        entry = {"date": dt, "dataset": "lst"}
        entry["lst_mean"] = float(np.nanmean(lst_stack[i]))
        entry["lst_max"] = float(np.nanmax(lst_stack[i]))
        combined_data.append(entry)

df = pd.DataFrame(combined_data)
print(f"Combined dataset: {len(df)} entries")
print(df.head(10))

# COMMAND ----------

# Multi-panel correlation figure
fig = make_subplots(
    rows=3, cols=1,
    shared_xaxes=True,
    subplot_titles=["🔥 Fire Detections", "🌿 Vegetation Health (NDVI)", "🌡️ Surface Temperature"],
    vertical_spacing=0.08,
)

# Fire
df_f = df[df["dataset"] == "fire"].dropna(subset=["fire_pixels"])
if not df_f.empty:
    fig.add_trace(
        go.Bar(x=df_f["date"], y=df_f["fire_pixels"], name="Fire pixels",
               marker_color="#e65100"),
        row=1, col=1,
    )

# NDVI
df_n = df[df["dataset"] == "ndvi"].dropna(subset=["ndvi_mean"])
if not df_n.empty:
    fig.add_trace(
        go.Scatter(x=df_n["date"], y=df_n["ndvi_mean"], mode="lines+markers",
                   name="Mean NDVI", line=dict(color="#2e7d32", width=3)),
        row=2, col=1,
    )

# Temperature
df_t = df[df["dataset"] == "lst"].dropna(subset=["lst_mean"])
if not df_t.empty:
    fig.add_trace(
        go.Scatter(x=df_t["date"], y=df_t["lst_mean"], mode="lines+markers",
                   name="Mean LST (°C)", line=dict(color="#d32f2f", width=3)),
        row=3, col=1,
    )

fig.update_layout(
    title="Multi-Dataset Correlation — Fire vs Vegetation vs Temperature",
    height=700, template="plotly_white",
    showlegend=True,
)
fig.update_yaxes(title_text="Fire pixels", row=1, col=1)
fig.update_yaxes(title_text="NDVI", row=2, col=1)
fig.update_yaxes(title_text="°C", row=3, col=1)
fig.show()

# COMMAND ----------

# Interactive Dash app — timeline slider across all datasets
app = Dash(__name__)

available_datasets = []
if fire_stack.size > 0:
    available_datasets.append("fire")
if ndvi_stack.size > 0:
    available_datasets.append("ndvi")
if lst_stack.size > 0:
    available_datasets.append("temperature")

def get_frame_figure(dataset, idx):
    if dataset == "fire" and fire_stack.size > 0:
        idx = min(idx, len(fire_timestamps) - 1)
        arr = np.where(fire_stack[idx] >= 7, fire_stack[idx], np.nan)
        fig = px.imshow(arr, color_continuous_scale="YlOrRd", zmin=7, zmax=9,
                       origin="upper", aspect="equal",
                       title=f"🔥 MODIS FireMask — {fire_timestamps[idx]}")
    elif dataset == "ndvi" and ndvi_stack.size > 0:
        idx = min(idx, len(ndvi_timestamps) - 1)
        fig = px.imshow(ndvi_stack[idx], color_continuous_scale="RdYlGn", zmin=-0.2, zmax=0.9,
                       origin="upper", aspect="equal",
                       title=f"🌿 MODIS NDVI — {ndvi_timestamps[idx]}")
    elif dataset == "temperature" and lst_stack.size > 0:
        idx = min(idx, len(lst_timestamps) - 1)
        fig = px.imshow(lst_stack[idx], color_continuous_scale="Hot",
                       zmin=float(np.nanpercentile(lst_stack, 5)),
                       zmax=float(np.nanpercentile(lst_stack, 95)),
                       origin="upper", aspect="equal",
                       title=f"🌡️ Land Surface Temp — {lst_timestamps[idx]}")
    else:
        fig = go.Figure()
        fig.add_annotation(text="No data available", xref="paper", yref="paper",
                          x=0.5, y=0.5, showarrow=False, font=dict(size=20))
    fig.update_layout(margin=dict(l=20, r=20, t=50, b=20), template="plotly_white")
    return fig

max_frames = max(
    len(fire_timestamps) if fire_stack.size > 0 else 0,
    len(ndvi_timestamps) if ndvi_stack.size > 0 else 0,
    len(lst_timestamps) if lst_stack.size > 0 else 0,
    1,
)

app.layout = html.Div([
    html.H2("🌍 Earth Datasets — Interactive Explorer", style={"textAlign": "center"}),
    html.Div([
        html.Label("Dataset:"),
        dcc.Dropdown(
            id="dataset-select",
            options=[{"label": d.title(), "value": d} for d in available_datasets],
            value=available_datasets[0] if available_datasets else "fire",
            style={"width": "200px", "display": "inline-block", "marginLeft": "10px"},
        ),
    ], style={"marginBottom": "10px", "textAlign": "center"}),
    dcc.Graph(id="main-graph", figure=get_frame_figure(available_datasets[0] if available_datasets else "fire", 0)),
    dcc.Slider(id="frame-slider", min=0, max=max_frames - 1, value=0, step=1,
               marks={i: str(i) for i in range(max_frames)}),
    html.Div([
        html.Button("▶ Play", id="play-btn", n_clicks=0),
        html.Button("⏸ Pause", id="pause-btn", n_clicks=0, style={"marginLeft": "8px"}),
    ], style={"marginTop": "10px", "textAlign": "center"}),
    dcc.Interval(id="playback-interval", interval=1000, n_intervals=0, disabled=True),
], style={"maxWidth": "900px", "margin": "0 auto", "padding": "20px"})


@app.callback(
    Output("main-graph", "figure"),
    Input("dataset-select", "value"),
    Input("frame-slider", "value"),
)
def update_figure(dataset, idx):
    return get_frame_figure(dataset or "fire", int(idx or 0))


@app.callback(
    Output("playback-interval", "disabled"),
    Input("play-btn", "n_clicks"),
    Input("pause-btn", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_play(_play, _pause):
    from dash import ctx
    if ctx.triggered_id == "play-btn":
        return False
    return True


@app.callback(
    Output("frame-slider", "value"),
    Input("playback-interval", "n_intervals"),
    Input("frame-slider", "value"),
)
def advance(n, current):
    return (int(current or 0) + 1) % max_frames


print(f"Dash app with datasets: {available_datasets}")
print("Starting on http://127.0.0.1:8062")
app.run(host="127.0.0.1", port=8062, debug=False)