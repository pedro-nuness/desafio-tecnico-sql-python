"""Composition root: both entry points served by `langgraph dev` share one container."""

from collections.abc import AsyncIterator

import pytest

from app.core.bootstrap import default_container, make_graph
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.graph.builder import ModernizationGraph


@pytest.fixture
async def fresh_default_container(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
    monkeypatch.delenv("LLM_CONFIG_FILE", raising=False)
    default_container.cache_clear()
    yield
    await default_container().close()
    default_container.cache_clear()


@pytest.mark.usefixtures("fresh_default_container")
async def test_api_and_langgraph_server_share_one_container_and_graph() -> None:
    container = default_container()

    assert default_container() is container  # FastAPI app (create_app)
    graph = await make_graph()  # langgraph.json factory
    assert graph is await container.get(ModernizationGraph)
    # The HTTP use case runs that same graph (no second engine / graph / Settings).
    service = await container.get(ModernizationService)
    assert service._pipeline._graph is graph  # type: ignore[attr-defined]
