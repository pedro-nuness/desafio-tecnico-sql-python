from typing import Protocol

from app.features.modernization.domain.models.modernization import Modernization, PipelineProgress


class ModernizationPipeline(Protocol):
    """Runs parsing -> semantic analysis -> generation -> validation and records the run.

    Keeps the use case independent of the orchestration framework (LangGraph lives
    in graph). Contract: the run is persisted as RUNNING before any step and with
    its final state at the end. Exceptions propagate; HTTP handlers record failures
    using the optional per-request progress snapshot.
    """

    async def run(
        self,
        *,
        source_code: str,
        schema_context: str | None,
        progress: PipelineProgress | None = None,
    ) -> Modernization: ...
