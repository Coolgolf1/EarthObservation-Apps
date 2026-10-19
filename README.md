# EarthObservation Apps

Three interactive maps for exploring satellite rasters and geographic features. A FastAPI backend serves the browser UIs, queries Microsoft Planetary Computer STAC imagery, aggregates raster pixels into H3 hexagons, and reads vector features from PostGIS. Optional integrations add natural-language search, travel-time areas, and Planet imagery search.

## Explore the apps

| App | Local URL | What it shows |
| --- | --- | --- |
| [EarthAnalyser](EarthAnalyser/README.md) | `/` | Fire, surface water, vegetation, land surface temperature, and land cover on a timeline, with H3 overlays and CSV export. |
| [EarthSearch](EarthSearch/README.md) | `/search/` | Place and travel-area searches with streamed vector features and optional LLM prompt parsing. |
| [EarthMapper](EarthMapper/README.md) | `/mapper/` | Viewport-based browsing and category filtering for Natural Earth and GADM features. |

The app navigation links connect all three views. The maps use OpenStreetMap tiles when no Mapbox token is configured; a token enables Mapbox satellite imagery.

## Stack and architecture

- **API:** FastAPI, Pydantic, psycopg, PostGIS, streaming NDJSON.
- **Earth observation:** Microsoft Planetary Computer STAC, rasterio, NumPy, H3.
- **Browser:** Leaflet and plain JavaScript modules, with no frontend build step.
- **Optional services:** OpenRouteService, Esri geocoding, OpenRouter-compatible LLM, Planet Data API, ImageCubes.

```text
Browser UIs  →  FastAPI routes  →  STAC / cloud-optimized GeoTIFFs
                            ↘  PostGIS (Natural Earth + GADM)
                            ↘  optional geocoding, routing, LLM, Planet APIs
```

## Run locally

You need Python 3.14+, [uv](https://docs.astral.sh/uv/), and internet access for package installation, map tiles, and STAC imagery. From the repository root:

```bash
cp .env.example .env
uv sync --project backend --frozen
uv run --project backend --frozen uvicorn backend.app.main:app --reload
```

Open <http://127.0.0.1:8000/> for EarthAnalyser, <http://127.0.0.1:8000/search/> for EarthSearch, <http://127.0.0.1:8000/mapper/> for EarthMapper, or <http://127.0.0.1:8000/docs> for the API schema. `./launch_backend.sh` starts the same development server.

The pages and `/health` load without credentials. Raster analysis needs access to the Planetary Computer STAC service. Vector search and EarthMapper require a prepared PostGIS database; the database contents are **not** included in this repository. The API queries `public.natural_earth_silver` and `public.gadm_silver` with the columns used in [`backend/app/routers/features.py`](backend/app/routers/features.py). Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_TOKEN` in `.env` before using those features. `/health/db` reports database connectivity.

### Optional integrations

| Variable | Enables |
| --- | --- |
| `MAPBOX_TOKEN` | Satellite and light basemaps; OpenStreetMap tiles are used without it. |
| `ESRI_API_KEY` | Esri geocoding; some queries can use Nominatim as a fallback. |
| `OSM_ORS_API_KEY` | OpenRouteService travel-time isochrones. |
| `LLM_API_KEY` | AI analysis and prompt parsing. EarthAnalyser uses OpenRouter; EarthSearch supports OpenRouter or OpenAI through `LLM_API_URL` and `LLM_MODEL`. |
| `PLANET_API_KEY` | Planet Data API proxy at `/planet`. |
| `IMAGECUBES_API_URL`, `IMAGECUBES_TOKEN` | Optional ImageCubes workflow. |

Use [`.env.example`](.env.example) as a template and leave integrations you do not use empty. `.env` is ignored by Git; never commit real credentials. The public `/health/config` endpoint returns the Mapbox browser token, so use a token restricted for public map use. If deploying a public site, choose a tile provider appropriate for the expected traffic and follow the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

## API and checks

FastAPI's `/docs` page is the source for request and response schemas. Main route groups are `/datasets` for H3 raster analysis, `/features` for PostGIS vector data and streamed search, `/planet` for the optional Planet proxy, and `/health` for service checks.

```bash
uv run --project backend --frozen pytest backend/tests
uv run --project backend --frozen ruff check backend/app backend/tests
```

The unit tests cover geometry, models, dataset configuration, raster helpers, and STAC helpers without live credentials. The larger scripts and notebooks in `EarthAnalyser/` and `EarthSearch/` are exploratory work and may need their own dependencies or external services.

GitHub Actions runs the backend tests, Ruff lint, and frontend JavaScript syntax checks on pushes and pull requests.

## Repository layout

```text
backend/                 FastAPI app, tests, and exploration scripts
EarthAnalyser/frontend/  Raster timeline and analysis UI
EarthSearch/frontend/    Search UI
EarthMapper/frontend/    Vector feature browser
EarthAnalyser/*.ipynb    Raster exploration notebooks
EarthSearch/notebooks/   Search and performance notebooks
```

## License

[MIT](LICENSE)
