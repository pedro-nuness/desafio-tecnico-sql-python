"""Use cases of the feature: one class each, one public `async def execute(command)`.

Entry points (routes, scripts) depend on these only. Failed runs are already recorded by the
graph; exceptions propagate to the global HTTP handlers.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.features.modernization.domain.evaluation import Evaluation, EvaluationSummary
from app.features.modernization.domain.modernization import Modernization, PipelineProgress
from app.features.modernization.evaluation.equivalence import EquivalenceMetric
from app.features.modernization.evaluation.repository import EvaluationRepository
from app.features.modernization.graph.builder import ModernizationGraph, run_modernization
from app.features.modernization.persistence.repository import ModernizationRepository
from app.shared.persistence import TransactionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModernizeCommand:
    source_code: str
    schema_context: str | None = None


@dataclass(frozen=True, slots=True)
class GetModernizationQuery:
    execution_id: UUID


@dataclass(frozen=True, slots=True)
class EvaluateCommand:
    execution_id: UUID


@dataclass(frozen=True, slots=True)
class EvaluationSummaryQuery:
    """No filters yet: the latest evaluation of every routine."""


class ModernizeRoutine:
    """Runs the pipeline (graph) for one routine; the graph records the run."""

    def __init__(self, graph: ModernizationGraph) -> None:
        self._graph = graph

    async def execute(
        self, command: ModernizeCommand, *, progress: PipelineProgress | None = None
    ) -> Modernization:
        finished = await run_modernization(
            self._graph,
            source_code=command.source_code,
            schema_context=command.schema_context,
            progress=progress,
        )
        logger.info(
            "modernization finished",
            extra={"execution_id": str(finished.id), "status": finished.status.value},
        )
        return finished


class GetModernization:
    def __init__(
        self, transactions: TransactionManager, modernizations: ModernizationRepository
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations

    async def execute(self, query: GetModernizationQuery) -> Modernization:
        async with self._transactions.transaction():  # read-only: nothing to commit
            return await self._modernizations.get(query.execution_id)


class EvaluateModernization:
    """Runs the behavioral-equivalence metric on a recorded execution and stores the result."""

    def __init__(
        self,
        transactions: TransactionManager,
        modernizations: ModernizationRepository,
        evaluations: EvaluationRepository,
        metric: EquivalenceMetric,
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations
        self._evaluations = evaluations
        self._metric = metric

    async def execute(self, command: EvaluateCommand) -> Evaluation:
        async with self._transactions.transaction():  # read-only: nothing to commit
            modernization = await self._modernizations.get(command.execution_id)
        # Outside any transaction: the cases take seconds and run on their own database.
        evaluation = Evaluation.of(modernization, await self._metric.evaluate(modernization))
        async with self._transactions.transaction() as tx:
            await self._evaluations.save(evaluation)
            await tx.commit()
        logger.info(
            "modernization evaluated",
            extra={
                "execution_id": str(modernization.id),
                "procedure": evaluation.procedure_name,
                "score": evaluation.score,
            },
        )
        return evaluation


class GetEvaluationSummary:
    def __init__(self, transactions: TransactionManager, evaluations: EvaluationRepository) -> None:
        self._transactions = transactions
        self._evaluations = evaluations

    async def execute(self, query: EvaluationSummaryQuery) -> EvaluationSummary:
        async with self._transactions.transaction():  # read-only: nothing to commit
            return EvaluationSummary(evaluations=await self._evaluations.latest_per_procedure())
