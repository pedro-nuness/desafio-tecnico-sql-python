from typing import Protocol

from app.features.modernization.domain.models.modernization import Modernization


class ModernizationPipeline(Protocol):
    """Runs parsing -> semantic analysis -> generation -> validation and records the run.

    Keeps the use case independent of the orchestration framework (LangGraph lives
    in graph). Contract: the run is persisted as RUNNING before any step and with
    its final state at the end; step failures are reported in the returned aggregate,
    never raised. Only persistence failures raise.
    """

    async def run(self, *, source_code: str, schema_context: str | None) -> Modernization: ...
