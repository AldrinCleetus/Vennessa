# Project Venessa

> **Built for Google Startup Buildathon India 2026 (Kochi Chapter)**  
> *Pre-mortem simulator for payment systems powered by Gemini and discrete-event simulation.*

Project Venessa pairs an **AI Adversary (Gemini)** with a **Deterministic Simulation Engine (SimPy)** to find payment failure modes (retry storms, gateway timeouts, non-idempotent duplicate debits) and quantify their rupee loss before launching code.

---

## Live Demo

- **Landing page:** [https://aldrincleetus.github.io/Venessa-Ascension/](https://aldrincleetus.github.io/Venessa-Ascension/)
- **Hosted app:** [https://vennessa-1.onrender.com/](https://vennessa-1.onrender.com/)

> The app is hosted on Render. Free-tier services spin down after inactivity, so the first request may take 30–60 seconds to wake up. See [RENDER_DEPLOY.md](RENDER_DEPLOY.md) for deployment details.

---

## Repository Structure

```
project_venessa/
├── backend/                   # Python FastAPI service & simulation core
│   ├── app/
│   │   ├── main.py            # FastAPI entry point
│   │   ├── api/routes.py      # API endpoints (/simulate, /analyze, /adversary)
│   │   ├── core/              # Discrete-event engine, models, static analyzer
│   │   └── ai/                # Vertex AI / Gemini integration
│   ├── models/                # Architecture definitions (YAML)
│   ├── scenarios/             # Failure scenario definitions (YAML)
│   ├── tests/                 # 23 discrete-event simulation test cases
│   └── Dockerfile             # Production container for Google Cloud Run
│
└── frontend/                  # Interactive Web UI (React + TypeScript + Vite)
    ├── src/
    │   ├── components/        # Architecture Graph, Sim View, Pre-mortem Report
    │   ├── App.tsx            # Main application layout & flow
    │   └── main.tsx
    ├── package.json
    └── vite.config.ts
```

---

## Quickstart

### 1. Backend (FastAPI + Simulation Engine)

```bash
cd backend

# Create virtual environment and install dependencies using uv
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
pytest

# Start development API server
uvicorn app.main:app --reload --port 8080
```

The API will be live at `http://localhost:8080`. Interactive documentation at `http://localhost:8080/docs`.

### 2. Frontend (Vite + React)

```bash
cd frontend
bun install   # or npm install
bun dev       # or npm run dev
```

The frontend will be live at `http://localhost:5173`.

---

## Deploying to Google Cloud Run

Project Venessa is built to deploy natively to **Google Cloud Run** in `asia-south1` (Mumbai) to adhere to Indian financial data residency requirements:

```bash
cd backend
gcloud run deploy project-venessa-api \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --set-env-vars GOOGLE_CLOUD_PROJECT=<YOUR_PROJECT_ID>
```
