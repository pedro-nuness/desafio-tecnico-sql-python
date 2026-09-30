# No `# syntax=` directive: the built-in Dockerfile frontend already supports
# `RUN --mount=type=cache`, and an external frontend image is one more thing that can fail.
FROM python:3.14-slim

RUN pip install --no-cache-dir uv==0.12.21

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer), then the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY alembic.ini langgraph.json ./
COPY migrations ./migrations
COPY app ./app

# /app writable by appuser: `langgraph dev` keeps its local state in /app/.langgraph_api.
RUN useradd --create-home --uid 10001 appuser && chown appuser /app
USER appuser

EXPOSE 8000
# LangGraph CLI server (as the challenge requires): the `modernization` graph, its API/Studio
# and our FastAPI routes (/health, /modernize) mounted via `http.app` in langgraph.json.
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "8000", "--no-browser", "--no-reload", "--allow-blocking"]
