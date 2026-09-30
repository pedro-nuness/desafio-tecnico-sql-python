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
from app.bootstrap import Container, default_container

type ContainerFactory = Callable[[], Container]


def create_app(container_factory: ContainerFactory = default_container) -> FastAPI:
    """`default_container` is shared with the LangGraph server (`make_graph`); tests inject
    their own factory."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = container_factory()
        logging.basicConfig(level=container.settings.log_level)
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
