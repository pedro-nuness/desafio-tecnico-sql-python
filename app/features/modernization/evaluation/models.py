import uuid
from datetime import datetime
from typing import Self

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.features.modernization.evaluation.domain import Evaluation
from app.features.modernization.validation.checks.behavior.domain import CaseResult


class EvaluationResultModel(Base):
    """One evaluation of one execution. Counters are columns (queryable); cases are JSONB."""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        Index("ix_evaluation_results_procedure_created_at", "procedure_name", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    modernization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modernization_history.id", ondelete="CASCADE")
    )
    procedure_name: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    static_valid: Mapped[bool] = mapped_column(Boolean)
    completed: Mapped[bool] = mapped_column(Boolean)
    cases_passed: Mapped[int] = mapped_column(Integer)
    cases_total: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    cases: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @classmethod
    def from_domain(cls, evaluation: Evaluation) -> Self:
        return cls(
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

    def to_domain(self) -> Evaluation:
        return Evaluation(
            id=self.id,
            modernization_id=self.modernization_id,
            procedure_name=self.procedure_name,
            metric=self.metric,
            prompt_version=self.prompt_version,
            model=self.model,
            static_valid=self.static_valid,
            completed=self.completed,
            cases=tuple(CaseResult.model_validate(case) for case in self.cases),
            created_at=self.created_at,
        )
