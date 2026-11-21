# Deploying Vennessa to Render

The repo is configured for two Render services:

1. **Web Service** — FastAPI backend in a Docker container.
2. **Static Site** — Vite/React frontend.

You can either use the included `render.yaml` Blueprint or configure the services manually with the settings below.

## 1. Backend Web Service

| Setting | Value |
| --- | --- |
| Runtime | Docker |
| Root Directory | `backend` |
| Dockerfile Path | `./Dockerfile` |
| Health Check Path | `/health` |
| Plan | Free (or paid to avoid cold starts) |

The Dockerfile binds `0.0.0.0` and uses Render's injected `PORT`:

```dockerfile
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\""]
```

Do **not** hardcode `PORT=8080` in Render. The default `10000` only applies when running the container locally.

### Backend environment variables

Set these in the Render dashboard (or fill them in when the Blueprint prompts you):

| Variable | Example / notes |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | Your GCP project ID for Vertex AI. |
| `GOOGLE_CLOUD_LOCATION` | `asia-south1` (Vertex only; unrelated to Render's region). |
| `GEMINI_MODEL` | `gemini-3.5-flash` |
| `GEMINI_API_KEY` | Google AI Studio key. Keep this secret. |
| `GEMINI_API_MODEL` | Usually the same as `GEMINI_MODEL`. |
| `GEMINI_PREFER_API_KEY` | `false` to try Vertex first, `true` to try the API key first. |
| `CORS_ORIGINS` | JSON array or comma list containing the exact frontend origin, e.g. `["https://vennessa-web.onrender.com"]`. |
| `ENVIRONMENT` | `production` |

If no Gemini provider is reachable, AI endpoints return a deterministic local fallback and include a warning in the response. Real Gemini generation requires valid ADC or a working API key.

## 2. Frontend Static Site

| Setting | Value |
| --- | --- |
| Root Directory | `frontend` |
| Build Command | `npm ci && npm run build` |
| Publish Directory | `dist` |
| Plan | Free |

Set this build-time environment variable:

| Variable | Value |
| --- | --- |
| `VITE_API_URL` | `https://<your-backend>.onrender.com` |

`VITE_API_URL` must be the **backend origin only** — no trailing `/api` and no trailing slash. Vite bakes it into the JS bundle at build time, so **redeploy the frontend after changing it**.

The included `frontend/public/_redirects` file provides the SPA fallback:

```text
/*    /index.html   200
```

## 3. Deploy order

1. Create the backend Web Service first.
2. Copy its URL, e.g. `https://vennessa-api.onrender.com`.
3. Create the frontend Static Site with `VITE_API_URL` pointing at that URL.
4. Update the backend's `CORS_ORIGINS` with the frontend URL and restart/redeploy the backend.
5. Visit the frontend URL and check the browser console for CORS or mixed-content errors.

If using the Blueprint, Render will prompt for the `sync: false` values. Set `VITE_API_URL` and `CORS_ORIGINS` after both service URLs are known, then redeploy both. The frontend entry uses `type: web` with `runtime: static` and `staticPublishPath: dist`, which is Render's Blueprint syntax for a Static Site.

## 4. Gotchas

- Free-tier Web Services spin down after inactivity; the first request can take 30–60 seconds.
- Gemini calls plus a SimPy run can be slow on a cold start. Use a paid instance or a warm-up approach if demos need instant response.
- `asia-south1` applies only to Vertex AI calls, not to where Render runs the container or static site.
- CORS origins must match exactly, including `https://` and no trailing slash.
- The backend still mounts `frontend/dist` when it exists, but the Render Docker image intentionally contains only the backend; the frontend is deployed separately as a Static Site.

## 5. Local verification before deploying

```bash
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8080
```

In another terminal:

```bash
cd frontend
npm run build
npm run dev
```

With `VITE_API_URL` empty, the dev server continues to proxy `/api` to `http://127.0.0.1:8080`.
