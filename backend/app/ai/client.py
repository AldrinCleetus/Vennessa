"""Gemini/Vertex AI client setup with an API-key fallback.

Primary path: Vertex AI using Application Default Credentials (ADC).
Fallback path: the Gemini API using ``GEMINI_API_KEY`` (AI Studio key).

Add to backend/.env:
    GEMINI_API_KEY=your-key-here
    # Optional: only needed if the Gemini API uses a different model name
    GEMINI_API_MODEL=gemini-3.5-flash
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

# Load environment variables from .env if present. Walk up a bounded number
# of ancestors so this works both from a source checkout and inside the
# Docker image, where the path is much shallower (``/app/app/ai/client.py``).
for _parent in list(Path(__file__).resolve().parents)[:4]:
    _env_candidate = _parent / ".env"
    if _env_candidate.exists():
        load_dotenv(_env_candidate)
        break

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "boreal-union-515307-d9")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
API_MODEL = os.getenv("GEMINI_API_MODEL", DEFAULT_MODEL)
# Prefer the Gemini API key by default. Vertex AI is only attempted when no
# API key is configured (e.g. on GCP, where ADC is available automatically).
PREFER_API_KEY = os.getenv("GEMINI_PREFER_API_KEY", "true").lower() in {"1", "true", "yes"}


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


PROVIDER_TIMEOUT_S = 25.0


async def generate_content(
    prompt: str,
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    """Generate text with Vertex AI, falling back to a Gemini API key.

    ``temperature`` (0.2-0.3 for structured fix proposals) and ``json_mode``
    constrain generation for callers that need stable, machine-readable output.
    The fallback triggers for any provider error (missing ADC, permission
    denied, quota exhausted, unknown model, etc.). Validation of the returned
    YAML/JSON happens in the calling module and is not retried here.

    Raises:
        RuntimeError: if no provider is configured, or every provider fails.
    """
    config = None
    if temperature is not None or json_mode:
        config = genai_types.GenerateContentConfig(
            **({"temperature": temperature} if temperature is not None else {}),
            **({"response_mime_type": "application/json"} if json_mode else {}),
        )
    attempts: list[tuple[str, Callable[[], genai.Client], Callable[[], str]]] = []

    if PREFER_API_KEY and API_KEY:
        # Gemini-only path: do not waste a provider attempt on Vertex when the
        # user has explicitly opted into the API key (local dev by default).
        attempts.append(("Gemini API key", get_api_key_client, get_api_key_model_name))
    else:
        # No API key (or Vertex preferred): Vertex first, API key as fallback.
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
            kwargs: dict[str, Any] = {"model": model_name_fn(), "contents": prompt}
            if config is not None:
                kwargs["config"] = config
            response = await asyncio.wait_for(
                client.aio.models.generate_content(**kwargs),
                timeout=PROVIDER_TIMEOUT_S,
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
