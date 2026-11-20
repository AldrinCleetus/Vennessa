"""Vertex AI Client setup and configuration for Project Venessa."""

import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai

# Load environment variables from .env if present
env_path = Path(__file__).resolve().parents[2] / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    # Also check root of repository
    root_env = Path(__file__).resolve().parents[4] / ".env"
    if root_env.exists():
        load_dotenv(root_env)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "boreal-union-515307-d9")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


def get_vertex_client() -> genai.Client:
    """Returns an authenticated Google GenAI Client configured for Vertex AI."""
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
    )


def get_model_name() -> str:
    """Returns the configured Gemini model name."""
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
