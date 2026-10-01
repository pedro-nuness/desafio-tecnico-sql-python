import logging
from uuid import UUID

from app.features.modernization.application.ports.pipeline.modernization_pipeline import (
    ModernizationPipeline,
)
from app.features.modernization.application.ports.repositories.modernization_repository import (
    ModernizationRepository,
)
from app.features.modernization.domain.models.modernization import (
    Modernization,
    PipelineProgress,
)
from app.shared.errors import NotFoundError
from app.shared.persistence import TransactionManager

logger = logging.getLogger(__name__)


class ModernizationService:
    """Use cases: modernize one routine, and read a recorded execution.

    The graph records every run (normal completion or the step that raised); exceptions
    then propagate to the global HTTP handlers.
    """

    def __init__(
        self,
        pipeline: ModernizationPipeline,
        transactions: TransactionManager,
        modernizations: ModernizationRepository,
    ) -> None:
        self._pipeline = pipeline
        self._transactions = transactions
        self._modernizations = modernizations

    async def modernize(
        self,
        source_code: str,
        schema_context: str | None = None,
        *,
        progress: PipelineProgress | None = None,
    ) -> Modernization:
        finished = await self._pipeline.run(
            source_code=source_code,
            schema_context=schema_context,
            progress=progress,
        )
        logger.info(
            "modernization finished",
            extra={"execution_id": str(finished.id), "status": finished.status.value},
        )
        return finished

    async def get(self, modernization_id: UUID) -> Modernization:
        async with self._transactions.transaction():  # read-only: nothing to commit
            modernization = await self._modernizations.find_by_id(modernization_id)
        if modernization is None:
            raise NotFoundError(
                f"Modernization {modernization_id} not found", execution_id=str(modernization_id)
            )
        return modernization
