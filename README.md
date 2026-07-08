# EPS Alvaro Perez - Monorepo

A comprehensive geospatial data analysis and visualization platform with multiple services.

## Project Structure

```
eps-alvaro-perez/
├── backend/                    # FastAPI backend for EarthMapper
│   └── app/
│       ├── main.py            # FastAPI application entry point
│       ├── core/              # Core utilities
│       │   ├── db.py          # Database connection
│       │   └── utils.py       # Utility functions
│       ├── models/            # Data models
│       │   ├── bbox.py        # Bounding box model
│       │   └── features.py    # GeoJSON feature models
│       └── routers/           # API endpoints
│           ├── health.py      # Health check endpoints
│           └── features.py    # Feature search endpoints
├── EarthMapper/               # Data analysis project - GADM/Natural Earth datasets
│   ├── pyproject.toml
│   ├── frontend/              # Interactive visualization
│   ├── notebooks/             # Analysis notebooks
│   └── docs/
├── EarthSearch/               # Data search and analysis project
│   ├── pyproject.toml
│   ├── frontend/              # Interactive search interface
│   ├── notebooks/             # Analysis notebooks
│   ├── examples/              # Example scripts
│   └── docs/
├── .env                       # Environment variables (shared across all services)
├── .env.example               # Template for environment variables
└── README.md                  # This file
```

## Setup

### Prerequisites
- Python 3.14+
- Virtual environment manager (venv, conda, etc.)

### Configuration

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

2. Configure the following environment variables:
   - **Database**: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_TOKEN`
   - **API Keys**: `ESRI_API_KEY`, `OSM_ORS_API_KEY`

### Backend Setup

The project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

```bash
cd backend
uv sync                              # install dependencies from pyproject.toml
uv run uvicorn app.main:app --reload
```

The backend API is available at `http://localhost:8000`. It also serves the
EarthAnalyser UI at `/` and the EarthSearch UI at `/search`.

### Services

#### EarthMapper
- **Purpose**: Query geospatial datasets (GADM, Natural Earth) and visualize on leaflet maps
- **Tech**: FastAPI backend, Python data processing
- **Usage**: See `EarthMapper/README.md`

#### EarthSearch
- **Purpose**: Search and analyze geospatial data with time-based capabilities
- **Tech**: Jupyter notebooks, Python analysis
- **Usage**: See `EarthSearch/README.md`

## API Endpoints

Interactive OpenAPI docs are available at `http://localhost:8000/docs`.

### Health (`/health`)
- `GET /health/` - Service status
- `GET /health/db` - Database connection status
- `GET /health/config` - Public frontend config (Mapbox token)

### Features (`/features`)
- `GET /features` - Query features in a bbox
- `GET /features/search-mode` - Place + reach-area feature search
- `POST /features/agent-search` - Natural-language (agentic) search
- `GET /features/stream` - Streaming (NDJSON) feature results

### Datasets (`/datasets`)
- `POST /datasets/h3` and `POST /datasets/h3/stream` - H3 raster datasets (fire, flood, vegetation, weather, land use)
- `POST /datasets/reach-area`, `POST /datasets/geocode`, `POST /datasets/classify` - search helpers
- `POST /datasets/analyse` - LLM analysis of a data report

### Planet (`/planet`)
- `GET /planet/health`, `POST /planet/quick-search` - Planet Data API proxy

## Environment Variables

All services use a shared `.env` file at the repository root:

| Variable | Purpose |
|----------|---------|
| `DB_HOST` | Database host |
| `DB_PORT` | Database port |
| `DB_NAME` | Database name |
| `DB_USER` | Database user |
| `DB_SCHEMA` | Database schema |
| `DB_TABLE` | Default table |
| `DB_SSLMODE` | SSL mode for connections |
| `DB_TOKEN` | Database authentication token |
| `ESRI_API_KEY` | ESRI/ArcGIS API key |
| `OSM_ORS_API_KEY` | OpenStreetMap/OpenRouteService API key |

## Development

### Backend Development
```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

### Data Analysis
```bash
cd EarthMapper
jupyter notebook  # or jupyter lab
```

## CORS Configuration

The backend allows requests from:
- `http://127.0.0.1:5500` (Live Server)
- `http://localhost:5500`
- `http://127.0.0.1:5600` (Alternative port)
- `http://localhost:5600`
- `http://127.0.0.1:5173` (Vite dev server)
- `http://localhost:5173`

## License

See individual project folders for specific licenses.
