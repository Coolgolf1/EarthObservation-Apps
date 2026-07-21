# EarthObservation Apps

A geospatial data analysis and visualization platform built with FastAPI, Leaflet, and H3 hexagonal aggregation. Combines satellite imagery from Microsoft Planetary Computer with vector data from PostGIS to enable interactive earth observation workflows.

## Services

| Service | Description |
|---------|-------------|
| **Backend** | FastAPI server — raster datasets (fire/flood/vegetation/weather/land-use via STAC), vector features (GADM + Natural Earth from PostGIS), LLM-powered search, Planet API proxy |
| **EarthAnalyser** | Interactive map UI with temporal H3 hexagon rendering, AI chat, and timeline playback |
| **EarthSearch** | Agentic map search — one natural-language prompt triggers geocoding, isochrone computation, and streamed NDJSON features |
| **EarthMapper** | Viewport-driven feature browser with category filtering on a Leaflet map |

## Project Structure

```
├── backend/              # FastAPI application
│   └── app/
│       ├── main.py       # Entry point (serves all frontends as static files)
│       ├── core/         # Config, DB, STAC, raster, H3, logging
│       ├── models/       # Pydantic models (bbox, GeoJSON features)
│       └── routers/      # API routers (datasets, features, health, planet)
├── EarthAnalyser/        # Main frontend (served at /)
├── EarthSearch/          # Search frontend (served at /search/)
├── EarthMapper/          # Mapper frontend (served at /mapper/)
└── .env.example          # Required environment variables
```

## Quick Start

### Prerequisites

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/) package manager
- PostgreSQL/PostGIS database with GADM and Natural Earth data

### Setup

```bash
# Clone and configure
cp .env.example .env
# Edit .env with your credentials

# Install and run
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

The app is available at `http://localhost:8000` with interactive API docs at `/docs`.

## API Endpoints

### Health (`/health`)
- `GET /health/` — Service status
- `GET /health/db` — Database connection check
- `GET /health/config` — Public frontend config (Mapbox token)

### Datasets (`/datasets`)
- `POST /datasets/h3` — H3-aggregated raster data (fire, flood, vegetation, weather, land-use)
- `POST /datasets/h3/stream` — Streaming variant (NDJSON)
- `POST /datasets/reach-area` — Isochrone computation via OpenRouteService
- `POST /datasets/geocode` — Geocoding via Esri/Nominatim
- `POST /datasets/analyse` — LLM analysis of data reports

### Features (`/features`)
- `GET /features` — Query features in a bounding box
- `GET /features/search-mode` — Place + reach-area search
- `POST /features/agent-search` — Natural-language agentic search
- `GET /features/stream` — Streaming NDJSON features

### Planet (`/planet`)
- `GET /planet/health` — Planet API connectivity
- `POST /planet/quick-search` — Planet Data API proxy

## Environment Variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Purpose |
|----------|---------|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_TOKEN` | PostgreSQL/PostGIS connection |
| `ESRI_API_KEY` | Geocoding (ArcGIS) |
| `OSM_ORS_API_KEY` | Isochrone computation (OpenRouteService) |
| `MAPBOX_TOKEN` | Map tiles (Mapbox Satellite) |
| `LLM_API_KEY` | LLM-powered search (OpenRouter) |
| `PLANET_API_KEY` | Planet Data API |
| `IMAGECUBES_API_URL`, `IMAGECUBES_TOKEN` | Sentinel-2 NDVI via ImageCubes |

## Development

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload   # API + all frontends
uv run pytest tests/                    # unit tests
```

Linting is configured via [`ruff.toml`](ruff.toml).

## License

MIT
