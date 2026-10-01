"""Lifecycle of a run in the history table: started, completed, or interrupted.

Used by the graph (not only by the HTTP use case) because the graph has more than one entry
point: POST /modernize, the LangGraph API (/runs) and Studio. Each transition is committed on
its own: the RUNNING row exists before the slow, failure-prone LLM call, so a run leaves a
trace even if the process dies. Persistence errors are not caught: a run that cannot be
recorded must fail loudly.
"""

from uuid import UUID

from app.features.modernization.domain.modernization import (
    Modernization,
    ModernizationReport,
    PipelineError,
)
from app.features.modernization.persistence.repository import ModernizationRepository


class ExecutionLog:
    def __init__(self, modernizations: ModernizationRepository) -> None:
        self._modernizations = modernizations

    async def start(self, source_code: str, schema_context: str | None) -> Modernization:
        modernization = Modernization.start(source_code, schema_context)
        await self._modernizations.save(modernization)
        return modernization

    async def complete(
        self, execution_id: UUID, report: ModernizationReport, generated_code: str | None
    ) -> Modernization:
        running = await self._modernizations.get(execution_id)
        return await self._finish(running.complete(report, generated_code))

    async def fail(
        self,
        execution_id: UUID,
        report: ModernizationReport,
        generated_code: str | None,
        error: PipelineError,
    ) -> Modernization:
        running = await self._modernizations.get(execution_id)
        return await self._finish(running.fail(report, generated_code, error))

    async def _finish(self, finished: Modernization) -> Modernization:
        await self._modernizations.update(finished)
        return finished
