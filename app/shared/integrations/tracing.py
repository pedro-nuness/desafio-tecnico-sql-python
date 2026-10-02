"""Custom spans and scores on the current graph node's Langfuse trace.

Same mechanism as TracedLLM: LangChain callbacks inherit the running node's context, so the
spans nest under it. Outside a traced graph run (no callbacks) every call is a no-op.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForChainRun, AsyncRunManager
from langchain_core.runnables.config import ensure_config, get_async_callback_manager_for_config
from langfuse.langchain import CallbackHandler


class TracedFailure(Exception):
    """Marks a span as failed (ERROR level, the message as its status) without raising."""


def langfuse_observation(run: AsyncRunManager) -> Any | None:
    """The Langfuse observation of a traced run, for what callbacks cannot carry (scores, cost).

    The handler keeps the observation of each run it traces; langfuse 4 has no public accessor.
    """
    for handler in run.handlers:
        if isinstance(handler, CallbackHandler):
            return handler._runs.get(run.run_id)
    return None


class Span:
    def __init__(self, run: AsyncCallbackManagerForChainRun) -> None:
        self._run = run
        self.output: dict[str, Any] = {}
        """Set by the caller before the span closes."""

    async def child(
        self,
        name: str,
        *,
        inputs: dict[str, Any],
        output: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """A finished child span; with `error`, it is marked failed."""
        manager = self._run.get_child()
        child = await manager.on_chain_start({"name": name}, inputs, name=name)
        if error is None:
            await child.on_chain_end(output)
        else:
            await child.on_chain_error(TracedFailure(error))

    def score(self, name: str, value: float, *, comment: str) -> None:
        """A numeric score on this span (listed with the trace's scores)."""
        observation = langfuse_observation(self._run)
        if observation is not None:
            observation.score(name=name, value=value, comment=comment)


@asynccontextmanager
async def span(name: str, inputs: dict[str, Any]) -> AsyncIterator[Span]:
    """A span under the current node; closed with `Span.output`, or failed if the body raises."""
    manager = get_async_callback_manager_for_config(ensure_config())
    run = await manager.on_chain_start({"name": name}, inputs, name=name)
    traced = Span(run)
    # Needed: close the span on failures too, then let the error propagate.
    try:
        yield traced
    except BaseException as exc:
        await run.on_chain_error(exc)
        raise
    await run.on_chain_end(traced.output)
