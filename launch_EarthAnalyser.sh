#!/usr/bin/env sh
set -eu

# FastAPI serves EarthAnalyser at / along with the search and mapper UIs.
cd "$(dirname "$0")"
exec uv run --project backend --frozen uvicorn backend.app.main:app --reload
