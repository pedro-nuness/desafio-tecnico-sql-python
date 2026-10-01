"""FastAPI application factory and lifespan configuration."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.bootstrap import Container, default_container
from app.core.exception_handlers import register_exception_handlers
from app.features.health.routes import router as health_router
from app.features.modernization.api.routes import router as modernization_router

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
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(modernization_router)
    return app


app = create_app()
