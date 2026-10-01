import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


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
