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
FROM node:22.23.4-alpine AS frontend

WORKDIR /frontend

# Manifest first so a source-only change does not re-run the dependency install. The
# lockfile patterns are optional: npm's lockfile may not be committed yet, and a glob that
# matches nothing is fine as long as package.json matched.
COPY frontend/package.json frontend/package-lock.json* frontend/npm-shrinkwrap.json* ./

# `npm ci` is reproducible but requires a lockfile in sync with package.json; fall back to
# `npm install` when there is none, rather than failing the whole image build.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --no-audit --no-fund

COPY frontend/ ./

# Produces /frontend/dist — index.html with the pre-paint theme/locale bootstrap plus the
# hashed asset bundle. The interface uses local system fonts; no font CDN is required.
RUN npm run typecheck && npm test && npm run build


# =======================================================================================
# Stage 2 — the backend runtime.
# =======================================================================================
FROM python:3.12-alpine3.24 AS runtime

# DATA_DIR holds the SQLite database, downloaded signal images and Telethon session
# material; FRONTEND_DIST is where FastAPI looks for the built SPA. Both match app/config.py
# and the values in .env.example. Production credentials/signing key arrive at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/static

# Minimal runtime; an optional proxy CA is mounted only during networked build steps.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    apk upgrade --no-cache && apk add --no-cache ca-certificates libstdc++ libffi

RUN addgroup -g 10001 signalpilot \
 && adduser -D -H -u 10001 -G signalpilot signalpilot

WORKDIR /app

# Install the backend. This pulls in fastapi, uvicorn[standard], sqlalchemy[asyncio],
# aiosqlite, pydantic-settings, telethon, httpx, cryptography, pillow and anthropic.
# MetaTrader5 is intentionally not among them — it is Windows-only and lives in the sidecar.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/requirements.lock ./requirements.lock
COPY backend/app ./app
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --upgrade pip==26.2.2 && pip install --no-cache-dir -c requirements.lock . && python -m pip uninstall -y pip

# The SPA built in stage 1, served from FRONTEND_DIST.
COPY --from=frontend /frontend/dist ./static

# The data volume stays writable by the fixed non-root uid. Read-only production
# deployments must explicitly supply SECRET_KEY; writable local development can bootstrap it.
RUN mkdir -p /data \
 && chown -R signalpilot:signalpilot /app /data \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "128"]
