"""Gemini/Vertex AI client setup with an API-key fallback.

Primary path: Vertex AI using Application Default Credentials (ADC).
Fallback path: the Gemini API using ``GEMINI_API_KEY`` (AI Studio key).

Add to backend/.env:
    GEMINI_API_KEY=your-key-here
    # Optional: only needed if the Gemini API uses a different model name
    GEMINI_API_MODEL=gemini-3.5-flash
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from google import genai

logger = logging.getLogger(__name__)

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

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
API_MODEL = os.getenv("GEMINI_API_MODEL", DEFAULT_MODEL)
PREFER_API_KEY = os.getenv("GEMINI_PREFER_API_KEY", "").lower() in {"1", "true", "yes"}


def get_vertex_client() -> genai.Client:
    """Returns a Vertex AI client configured from the environment."""
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
    )


def get_api_key_client() -> genai.Client:
    """Returns a Gemini API client using an AI Studio API key."""
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return genai.Client(api_key=API_KEY)


def get_model_name() -> str:
    """Returns the model used for Vertex AI requests."""
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)


def get_api_key_model_name() -> str:
    """Returns the model used for Gemini API-key requests."""
    return os.getenv("GEMINI_API_MODEL", API_MODEL)


async def generate_content(prompt: str) -> str:
    """Generate text with Vertex AI, falling back to a Gemini API key.

    The fallback triggers for any provider error (missing ADC, permission
    denied, quota exhausted, unknown model, etc.). Validation of the returned
    YAML/JSON happens in the calling module and is not retried here.

    Raises:
        RuntimeError: if no provider is configured, or every provider fails.
    """
    attempts: list[tuple[str, Callable[[], genai.Client], Callable[[], str]]] = []

    if PREFER_API_KEY:
        if API_KEY:
            attempts.append(("Gemini API key", get_api_key_client, get_api_key_model_name))
        attempts.append(("Vertex AI", get_vertex_client, get_model_name))
    else:
        attempts.append(("Vertex AI", get_vertex_client, get_model_name))
        if API_KEY:
            attempts.append(("Gemini API key", get_api_key_client, get_api_key_model_name))

    if not attempts:
        raise RuntimeError(
            "No Gemini credentials configured. Set up Vertex AI ADC or add "
            "GEMINI_API_KEY to backend/.env."
        )

    last_error: Exception | None = None
    for provider_name, client_factory, model_name_fn in attempts:
        try:
            client = client_factory()
            response = await client.aio.models.generate_content(
                model=model_name_fn(),
                contents=prompt,
            )
            text = getattr(response, "text", None)
            if not text:
                raise RuntimeError(f"{provider_name} returned an empty response")
            return text
        except Exception as exc:  # fall through to the next provider
            last_error = exc
            logger.warning(
                "Gemini provider %s failed (%s); trying fallback",
                provider_name,
                type(exc).__name__,
            )

    raise RuntimeError(
        f"All Gemini providers failed. Last error: {last_error}"
    ) from last_error
