"""Composition root: both entry points served by `langgraph dev` share one container."""

from collections.abc import AsyncIterator

import pytest

from app.core.bootstrap import default_container, make_graph
from app.features.modernization.graph.builder import ModernizationGraph
from app.features.modernization.use_cases import ModernizeRoutine


@pytest.fixture
async def fresh_default_container(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
    monkeypatch.setenv("LLM_CONFIG_FILE", "")  # empty = single route from the LLM_* variables
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
    use_case = await container.get(ModernizeRoutine)
    assert use_case._graph is graph  # type: ignore[attr-defined]
