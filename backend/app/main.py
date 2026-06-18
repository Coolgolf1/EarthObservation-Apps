from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .routers.health import router as health_router
from .routers.features import router as features_router
from .routers.planet import router as planet_router

app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=[
		"http://127.0.0.1:5500",
		"http://localhost:5500",
		"http://127.0.0.1:5600",
		"http://localhost:5600",
		"http://127.0.0.1:5173",
		"http://localhost:5173",
	],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(features_router)
app.include_router(planet_router)

repo_root = Path(__file__).resolve().parents[2]
earthsearch_frontend_dir = repo_root / "EarthSearch" / "frontend"

if earthsearch_frontend_dir.exists():
	# Serve EarthSearch UI from root while preserving API routes.
	app.mount("/", StaticFiles(directory=earthsearch_frontend_dir, html=True), name="earthsearch_frontend")

