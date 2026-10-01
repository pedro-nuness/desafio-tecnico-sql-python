"""Composition root: both entry points served by `langgraph dev` share one container."""

from collections.abc import Iterator

import pytest

from app.core.bootstrap import default_container, make_graph


@pytest.fixture
def fresh_default_container(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
    default_container.cache_clear()
    yield
    default_container.cache_clear()


@pytest.mark.usefixtures("fresh_default_container")
def test_api_and_langgraph_server_share_one_engine_and_graph() -> None:
    container = default_container()

    assert default_container() is container  # FastAPI lifespan
    assert make_graph() is container.graph  # langgraph.json factory
    # The HTTP use case runs that same graph (no second engine / graph / Settings).
    assert container.modernization_service._pipeline._graph is container.graph  # type: ignore[attr-defined]
