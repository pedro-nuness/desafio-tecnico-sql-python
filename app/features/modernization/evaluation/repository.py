from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from app.core.database.transaction import SessionTransactionManager
from app.features.modernization.domain.evaluation import CaseResult, Evaluation
from app.features.modernization.evaluation.models import EvaluationResultModel


class EvaluationRepository(Protocol):
    """Port: evaluation results (in-memory fake in the tests). Works inside the caller's
    transaction; flushes, never commits."""

    async def save(self, evaluation: Evaluation) -> None: ...

    async def latest_per_procedure(self) -> tuple[Evaluation, ...]:
        """The most recent evaluation of each routine, ordered by routine name."""
        ...


class SqlAlchemyEvaluationRepository:
    def __init__(self, transactions: SessionTransactionManager) -> None:
        self._transactions = transactions

    async def save(self, evaluation: Evaluation) -> None:
        session = self._transactions.current_session()
        session.add(
            EvaluationResultModel(
                id=evaluation.id,
                modernization_id=evaluation.modernization_id,
                procedure_name=evaluation.procedure_name,
                metric=evaluation.metric,
                prompt_version=evaluation.prompt_version,
                model=evaluation.model,
                static_valid=evaluation.static_valid,
                completed=evaluation.completed,
                cases_passed=evaluation.cases_passed,
                cases_total=evaluation.cases_total,
                score=evaluation.score,
                cases=[case.model_dump(mode="json") for case in evaluation.cases],
                created_at=evaluation.created_at,
            )
        )
        await session.flush()

    async def latest_per_procedure(self) -> tuple[Evaluation, ...]:
        latest = (
            select(EvaluationResultModel)
            .ext(distinct_on(EvaluationResultModel.procedure_name))
            .order_by(EvaluationResultModel.procedure_name, EvaluationResultModel.created_at.desc())
        )
        models = (await self._transactions.current_session().scalars(latest)).all()
        return tuple(_to_domain(model) for model in models)


def _to_domain(model: EvaluationResultModel) -> Evaluation:
    return Evaluation(
        id=model.id,
        modernization_id=model.modernization_id,
        procedure_name=model.procedure_name,
        metric=model.metric,
        prompt_version=model.prompt_version,
        model=model.model,
        static_valid=model.static_valid,
        completed=model.completed,
        cases=tuple(CaseResult.model_validate(case) for case in model.cases),
        created_at=model.created_at,
    )
