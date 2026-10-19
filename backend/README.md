# EarthObservation API

FastAPI backend for the three browser UIs. It serves the frontends, raster analysis endpoints, vector feature queries, and optional geocoding, LLM, and Planet integrations.

From the repository root, copy `.env.example` to `.env`, fill in the services you intend to use, then run:

```bash
uv sync --project backend --frozen
uv run --project backend --frozen uvicorn backend.app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the API schema. The main [README](../README.md) lists the data and credential requirements for each UI.
