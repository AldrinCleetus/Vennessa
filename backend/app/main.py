from __future__ import annotations

import json
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router

app = FastAPI(
    title="Project Venessa API",
    description="Pre-mortem simulator for payment systems powered by Gemini and discrete-event simulation.",
    version="0.1.0",
)

def _cors_origins() -> tuple[list[str], bool]:
    """Read CORS settings from the environment.

    Accepts either a JSON array (``["https://app.onrender.com"]``) or a
    comma-separated list. Defaults to the local Vite/React dev servers.
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                origins = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                origins = [part.strip() for part in str(parsed).split(",") if part.strip()]
        except (TypeError, ValueError):
            origins = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        origins = ["http://localhost:5173", "http://localhost:3000"]

    if not origins:
        origins = ["http://localhost:5173", "http://localhost:3000"]

    allow_credentials = os.getenv("CORS_ALLOW_CREDENTIALS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if origins == ["*"]:
        # Browsers reject wildcard origins when credentials are enabled.
        allow_credentials = False
    return origins, allow_credentials


ALLOWED_ORIGINS, ALLOW_CREDENTIALS = _cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "project-venessa-api", "version": "0.1.0"}


# Serve static frontend build if dist folder exists
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
