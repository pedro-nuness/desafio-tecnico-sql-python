"""FastAPI application.

Served two ways, same code:
- mounted by the LangGraph server through `http.app` in langgraph.json (`langgraph dev`,
  Docker Compose), next to the LangGraph API and Studio. The server merges this app's lifespan;
- standalone: `uvicorn app.api.main:app` (only these routes, no LangGraph API/Studio).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health, modernization
from app.bootstrap import Container, build_container
from app.config.settings import Settings

type ContainerFactory = Callable[[Settings], Container]


def create_app(
    settings: Settings | None = None,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=resolved.log_level)
        container = container_factory(resolved)
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(title="PL/pgSQL Modernizer", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(modernization.router)
    return app


app = create_app()
