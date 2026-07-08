# EarthAnalyser

Multi-dataset geospatial analysis frontend.  Visualises fire, flood, vegetation, weather, and land-use data from Planetary Computer STAC on an interactive Leaflet map with H3 hexagonal aggregation.

## Running

```bash
cd backend && uv run uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` (served by FastAPI).
