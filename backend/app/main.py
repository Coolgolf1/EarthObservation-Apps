from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routers.datasets import router as datasets_router
from .routers.features import router as features_router
from .routers.health import router as health_router
from .routers.planet import router as planet_router

app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=[
		"http://127.0.0.1:5500",
		"http://localhost:5500",
		"http://127.0.0.1:5600",
		"http://localhost:5600",
		"http://127.0.0.1:5700",
		"http://localhost:5700",
		"http://127.0.0.1:5173",
		"http://localhost:5173",
		"http://127.0.0.1:8080",
		"http://localhost:8080",
		"http://127.0.0.1:8000",
		"http://localhost:8000",
	],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(features_router)
app.include_router(planet_router)
app.include_router(datasets_router)

repo_root = Path(__file__).resolve().parents[2]
earthsearch_frontend_dir = repo_root / "EarthSearch" / "frontend"
earthanalyser_frontend_dir = repo_root / "EarthAnalyser" / "frontend"
earthmapper_frontend_dir = repo_root / "EarthMapper" / "frontend"

if earthsearch_frontend_dir.exists():
	app.mount("/search", StaticFiles(directory=earthsearch_frontend_dir, html=True), name="earthsearch_frontend")

if earthmapper_frontend_dir.exists():
	app.mount("/mapper", StaticFiles(directory=earthmapper_frontend_dir, html=True), name="earthmapper_frontend")

if earthanalyser_frontend_dir.exists():
	# Serve EarthAnalyser as the root UI.
	app.mount("/", StaticFiles(directory=earthanalyser_frontend_dir, html=True), name="earthanalyser_frontend")

