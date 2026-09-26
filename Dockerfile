# --- build stage: resolve and install dependencies into a self-contained venv ---------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- runtime stage: slim image, non-root, one volume -------------------------------------------
FROM python:3.12-slim
RUN useradd --create-home --uid 1000 podium && mkdir -p /data && chown podium:podium /data
WORKDIR /app
COPY --from=builder --chown=podium:podium /app/.venv /app/.venv
COPY --chown=podium:podium alembic.ini entrypoint.sh ./
COPY --chown=podium:podium migrations ./migrations
COPY --chown=podium:podium fixtures ./fixtures
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PODIUM_DATA_DIR=/data \
    PODIUM_FIXTURES_PATH=/app/fixtures/fixtures.json
USER podium
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
ENTRYPOINT ["./entrypoint.sh"]
