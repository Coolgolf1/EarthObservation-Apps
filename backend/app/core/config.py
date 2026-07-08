"""Shared configuration constants for the backend."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(env_path)

# ─── Planetary Computer ──────────────────────────────────────
CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# ─── Memory Safety ───────────────────────────────────────────
MAX_CONCURRENT_RASTERS = 16
MAX_WINDOW_PIXELS = 50_000_000
MAX_H3_INPUT_PIXELS = 10_000_000
RASTER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RASTERS)

# ─── GDAL / rasterio ─────────────────────────────────────────
GDAL_ENV = {
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

# ─── Isochrone / Reach Area ──────────────────────────────────
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
    "foot-walking": 5.0,
    "cycling": 15.0,
    "cycling-regular": 16.0,
    "driving": 45.0,
    "driving-car": 45.0,
    "driving-hgv": 35.0,
}

# ─── Geocoding ────────────────────────────────────────────────
ESRI_GEOCODE_URL = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/World"
    "/GeocodeServer/findAddressCandidates"
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# ─── LLM ──────────────────────────────────────────────────────
LLM_API_URL_DEFAULT = "https://api.openai.com/v1/chat/completions"
OPENROUTER_API_URL_DEFAULT = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL_DEFAULT = os.getenv("LLM_MODEL", "qwen/qwen3-235b-a22b:free")

# ─── Dataset Configurations ──────────────────────────────────

DATASET_CONFIG = {
    "fire": {
        "collection": "modis-14A1-061",
        "asset": "FireMask",
        "description": "MODIS Daily Fire (14A1)",
        "value_type": "classification",
        "filter": lambda arr: (arr >= 7) & (arr <= 9),
        "classify": lambda val: (
            "high" if val >= 9 else "nominal" if val >= 8 else "low"
        ),
        "scale": None,
        "max_h3_res": 7,
    },
    "flood": {
        "collection": "jrc-gsw",
        "asset": "occurrence",
        "description": "JRC Global Surface Water",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "permanent" if val >= 80 else "seasonal" if val >= 30 else "ephemeral"
        ),
        "scale": None,
        "max_h3_res": 7,
    },
    "vegetation": {
        "collection": "modis-13A1-061",
        "asset": "500m_16_days_NDVI",
        "description": "MODIS NDVI (16-day)",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "dense" if val > 6000 else "moderate" if val > 3000
            else "sparse" if val > 1000 else "bare"
        ),
        "scale": 10000.0,
        "max_h3_res": 7,
    },
    "weather": {
        "collection": "modis-11A1-061",
        "asset": "LST_Day_1km",
        "description": "MODIS Daily Land Surface Temperature",
        "value_type": "continuous",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "extreme" if val > 16000 else "hot" if val > 15000
            else "warm" if val > 14000 else "cool"
        ),
        "scale": None,
        "max_h3_res": 7,
    },
    "landuse": {
        "collection": "esa-worldcover",
        "asset": "map",
        "description": "ESA WorldCover (10m)",
        "value_type": "classification",
        "filter": lambda arr: arr > 0,
        "classify": lambda val: (
            "tree" if val == 10 else "shrub" if val == 20 else "grass" if val == 30
            else "crop" if val == 40 else "built" if val == 50 else "bare" if val == 60
            else "water" if val == 80 else "other"
        ),
        "scale": None,
        "max_h3_res": 7,
    },
}
