FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=10000

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy the project definition and source before the editable install so
# hatchling can see the declared packages on a clean build.
COPY pyproject.toml .
COPY app/ ./app/
COPY models/ ./models/
COPY scenarios/ ./scenarios/

RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN uv pip install -e .

EXPOSE 10000

# Render injects PORT at runtime. Cloud Run does the same. The shell form
# expands it, while the default keeps local `docker run` predictable.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\""]
