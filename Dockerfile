# syntax=docker/dockerfile:1

# --- Build stage: install dependencies into an isolated venv with uv ---------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN pip install --no-cache-dir "uv>=0.8,<0.9"

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Install locked dependencies first; this layer is cached until the lockfile
# changes, so day-to-day source edits rebuild in seconds.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Then build + install the project itself into the same venv.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- Runtime stage: slim image with only the venv and the app ----------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

# Non-root user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# The application package is installed in the venv. Alembic config/scripts are
# read from the working tree (WORKDIR=/app) at runtime, so only those are copied.
COPY migrations ./migrations
COPY alembic.ini ./

RUN chown -R app:app /app
USER app

# Healthcheck: the scheduler writes a heartbeat every minute; this passes only
# while that heartbeat stays fresh.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "fintracker.healthcheck"]

CMD ["python", "-m", "fintracker.run"]
