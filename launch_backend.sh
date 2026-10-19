#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
exec uv run --project backend --frozen uvicorn backend.app.main:app --reload
