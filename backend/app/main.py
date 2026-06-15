from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers.health import router as health_router
from .routers.features import router as features_router

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

