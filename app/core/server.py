"""FastAPI application factory and lifespan configuration."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dishka import AsyncContainer
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from app.core.bootstrap import default_container
from app.core.config.settings import Settings
from app.core.exception_handlers import register_exception_handlers
from app.features.health.routes import router as health_router
from app.features.modernization.api.routes import router as modernization_router
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)


def create_app(container: AsyncContainer | None = None) -> FastAPI:
    """Uses the process-wide container shared with the LangGraph server (`make_graph`);
    tests pass their own."""
    container = container or default_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=(await container.get(Settings)).log_level)
        # Resolve the use case once at startup: a missing API key or a broken config fails
        # the boot instead of the first request.
        await container.get(ModernizationService)
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(title="PL/pgSQL Modernizer", version="0.1.0", lifespan=lifespan)
    setup_dishka(container, app)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(modernization_router)
    return app


app = create_app()
