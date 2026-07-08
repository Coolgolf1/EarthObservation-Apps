# EarthSearch

Agentic map search with one prompt.  Uses LLM-powered intent parsing, isochrone computation, and multi-category feature streaming over NDJSON.

## Running

```bash
cd backend && uv run uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/search/` (served by FastAPI).
