from typing import Protocol
from uuid import UUID

from app.domain.models.modernization import PipelineOutcome


class ModernizationPipeline(Protocol):
    """Runs parsing -> semantic analysis -> generation -> validation.

    Keeps the use case independent of the orchestration framework (LangGraph lives
    in app.graph). Implementations must never raise: failures are reported in the
    outcome together with every step that completed before them.
    """

    async def run(
        self,
        *,
        execution_id: UUID,
        source_code: str,
        schema_context: str | None,
    ) -> PipelineOutcome: ...
