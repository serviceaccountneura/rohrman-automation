# syntax=docker/dockerfile:1

# ── Builder: resolve dependencies into a self-contained virtualenv ───────────
FROM python:3.13-slim AS builder

# uv is what the project locks with, so the image installs from uv.lock rather
# than re-resolving. A rebuild then gets the same versions that were tested.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# application, so editing a route does not reinstall PyMuPDF.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# libgl / libglib are what PyMuPDF and Pillow link against for raster work.
# Without them the image builds fine and then fails at the first PDF render,
# which is a long way from the mistake.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Runs unprivileged. Nothing here needs root, and the container reaches Tekion
# and Vertex with real credentials.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# One process, deliberately. TEKION_LOCK is a threading.RLock, which serialises
# Tekion work only WITHIN a process -- a second worker or a second replica
# reintroduces the race that posts invoices to the wrong dealership.
#
# Concurrency comes from PIPELINE_WORKERS instead: those are threads in this
# process, so the lock still holds. Do not add --workers, and do not scale this
# service, until the client takes its dealership per call.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn api.main:app --host 0.0.0.0 --port 8000"]
