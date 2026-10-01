from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.features.modernization.domain.evaluation import Evaluation
from app.features.modernization.evaluation.models import EvaluationResultModel


class EvaluationRepository(Protocol):
    """Port: evaluation results (in-memory fake in the tests). Each save commits."""

    async def save(self, evaluation: Evaluation) -> None: ...

    async def latest_per_procedure(self) -> tuple[Evaluation, ...]:
        """The most recent evaluation of each routine, ordered by routine name."""
        ...


class SqlAlchemyEvaluationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, evaluation: Evaluation) -> None:
        async with self._sessions() as session:
            session.add(EvaluationResultModel.from_domain(evaluation))
            await session.commit()

    async def latest_per_procedure(self) -> tuple[Evaluation, ...]:
        latest = (
            select(EvaluationResultModel)
            .ext(distinct_on(EvaluationResultModel.procedure_name))
            .order_by(EvaluationResultModel.procedure_name, EvaluationResultModel.created_at.desc())
        )
        async with self._sessions() as session:
            models = (await session.scalars(latest)).all()
        return tuple(model.to_domain() for model in models)
