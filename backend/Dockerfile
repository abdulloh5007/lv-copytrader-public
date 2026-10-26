# syntax=docker/dockerfile:1
#
# LV Copytrader application image — one container, one port, one process.
#
# The dashboard is a Vite SPA built to static files and served by FastAPI itself, so this
# image is multi-stage: stage 1 builds the SPA with Node, stage 2 runs the Python backend
# and copies the built bundle in. The build therefore needs BOTH frontend/ and backend/,
# which is why docker-compose.yml sets the build context to the repository root (".") with
# `dockerfile: backend/Dockerfile` and every COPY below is written repo-root-relative.


# =======================================================================================
# Stage 1 — build the dashboard.
# =======================================================================================
FROM node:22-alpine AS frontend

WORKDIR /frontend

# Manifest first so a source-only change does not re-run the dependency install. The
# lockfile patterns are optional: npm's lockfile may not be committed yet, and a glob that
# matches nothing is fine as long as package.json matched.
COPY frontend/package.json frontend/package-lock.json* frontend/npm-shrinkwrap.json* ./

# `npm ci` is reproducible but requires a lockfile in sync with package.json; fall back to
# `npm install` when there is none, rather than failing the whole image build.
RUN npm ci --no-audit --no-fund

COPY frontend/ ./

# Produces /frontend/dist — index.html with the pre-paint theme/locale bootstrap plus the
# hashed asset bundle. The interface uses local system fonts; no font CDN is required.
RUN npm run typecheck && npm test && npm run build


# =======================================================================================
# Stage 2 — the backend runtime.
# =======================================================================================
FROM python:3.12-slim AS runtime

# DATA_DIR holds the SQLite database, downloaded signal images and Telethon session
# material; FRONTEND_DIST is where FastAPI looks for the built SPA. Both match app/config.py
# and the values in .env.example, so the container is correct even with no .env at all.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/static

# curl serves two purposes: the container healthcheck below, and the sidecar smoke test from
# BUILD_PROMPT's stage-1 check (`curl -H 'X-SP-Token: …' http://mt5-sidecar:18812/rpc …`),
# which is run from inside this container because that port is not published to the host.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root. The uid is fixed so the sp-data volume keeps stable ownership across rebuilds.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin signalpilot

WORKDIR /app

# Install the backend. This pulls in fastapi, uvicorn[standard], sqlalchemy[asyncio],
# aiosqlite, pydantic-settings, telethon, httpx, cryptography, pillow and anthropic.
# MetaTrader5 is intentionally not among them — it is Windows-only and lives in the sidecar.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/requirements.lock ./requirements.lock
COPY backend/app ./app
RUN pip install --no-cache-dir -c requirements.lock .

# The SPA built in stage 1, served from FRONTEND_DIST.
COPY --from=frontend /frontend/dist ./static

# /data is a mount point for the named volume; creating and chowning it here means a fresh
# volume inherits this ownership and the non-root process can write the database. /app must
# also be writable: when SECRET_KEY is missing, crypto.bootstrap_secret_key() generates one
# and writes it to /app/.env (app/config.py's container fallback for env_file_path).
RUN mkdir -p /data \
 && chown -R signalpilot:signalpilot /app /data

USER signalpilot

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-m", "app.cli.healthcheck"]

# Exactly one worker and no --reload, in every environment (BUILD_PROMPT §3.6):
#  * a second worker means a second Telethon client and a second ingest queue, so one
#    channel post would be processed twice and the same trade opened twice;
#  * --reload re-executes module import in a new event loop, and Telethon re-checks its
#    loop on every request — that is `RuntimeError: The asyncio event loop must not change
#    after connection`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
