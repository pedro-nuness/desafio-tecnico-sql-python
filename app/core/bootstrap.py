"""The dishka container shared by every entry point.

`default_container` is cached per process, so both entry points served by `langgraph dev`
(the FastAPI app and the LangGraph server via `make_graph`) resolve the same engine, LLM
gateway and graph. What gets built, and how, lives in providers.py. Tests and scripts call
`build_container` (optionally with providers that override some dependencies).
"""

from functools import cache

from dishka import AsyncContainer, Provider, make_async_container

from app.core.config.settings import Settings
from app.core.providers import InfrastructureProvider, ModernizationProvider
from app.features.modernization.graph.builder import ModernizationGraph


def build_container(settings: Settings | None = None, *overrides: Provider) -> AsyncContainer:
    """`overrides` win over the default providers for the types they provide."""
    return make_async_container(
        InfrastructureProvider(),
        ModernizationProvider(),
        *overrides,
        context={Settings: settings or Settings()},
    )


@cache
def default_container() -> AsyncContainer:
    """The process-wide container: one engine (connection pool), one graph, one Settings."""
    return build_container()


async def make_graph() -> ModernizationGraph:
    """Graph factory referenced by langgraph.json (LangGraph API / Studio).

    Same graph the FastAPI routes use, so runs started there are persisted the same way.
    """
    return await default_container().get(ModernizationGraph)
