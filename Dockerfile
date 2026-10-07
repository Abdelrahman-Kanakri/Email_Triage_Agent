# Email Triage Agent -- API + web UI in one container.
#
#   docker compose up --build        (reads .env, see .env.example)
#
# Two layers on purpose: dependencies (slow, cached until uv.lock changes)
# then app code (fast, changes often).

FROM python:3.13-slim

# uv binary pinned to the version the lockfile was produced with.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    LOG_TO_STDOUT=true \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1) Dependencies only -- cached until pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Application code + the sample inbox (compose mounts the real one over it).
COPY app ./app
COPY data/inbox ./data/inbox

# Run as non-root; it only needs to write logs, outbox and the checkpoint DB.
RUN useradd --create-home --uid 1000 triage \
    && mkdir -p logs data/outbox data/state \
    && chown -R triage:triage /app
USER triage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# One worker on purpose: per-thread run locks live in process memory, and
# SQLite is a single-writer store. Scale out only after moving both to
# shared infrastructure (Postgres checkpointer + a distributed lock).
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
