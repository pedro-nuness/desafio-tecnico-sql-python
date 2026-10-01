"""HTTP contract: requests become commands, domain results become responses."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.domain.evaluation import (
    CaseResult,
    Evaluation,
    EvaluationSummary,
)
from app.features.modernization.domain.modernization import Modernization, ModernizationReport
from app.features.modernization.use_cases import ModernizeCommand


class ModernizationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_code: str = Field(min_length=1, description="CREATE FUNCTION/PROCEDURE ... PL/pgSQL")
    schema_context: str | None = Field(
        default=None,
        alias="schema",
        description="Optional DDL of the tables involved (improves generation).",
    )

    def to_command(self) -> ModernizeCommand:
        return ModernizeCommand(source_code=self.source_code, schema_context=self.schema_context)


class ModernizationResponse(BaseModel):
    execution_id: UUID
    status: ModernizationStatus
    generated_code: str | None
    report: ModernizationReport
    """The domain report is already a serializable contract; reused instead of mirrored."""
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, modernization: Modernization) -> ModernizationResponse:
        return cls(
            execution_id=modernization.id,
            status=modernization.status,
            generated_code=modernization.generated_code,
            report=modernization.report,
            created_at=modernization.created_at,
            updated_at=modernization.updated_at,
        )


class EvaluationResponse(BaseModel):
    evaluation_id: UUID
    execution_id: UUID
    procedure_name: str
    metric: str
    prompt_version: str | None
    model: str | None
    score: float
    """Share of dataset cases where the generated code behaves like the original."""
    cases_passed: int
    cases_total: int
    equivalent: bool
    holdout_passed: int
    holdout_total: int
    holdout_equivalent: bool
    """Equivalent on the cases the repair loop never showed to the LLM."""
    static_valid: bool
    completed: bool
    cases: tuple[CaseResult, ...]
    created_at: datetime

    @classmethod
    def from_domain(cls, evaluation: Evaluation) -> EvaluationResponse:
        return cls(
            evaluation_id=evaluation.id,
            execution_id=evaluation.modernization_id,
            procedure_name=evaluation.procedure_name,
            metric=evaluation.metric,
            prompt_version=evaluation.prompt_version,
            model=evaluation.model,
            score=evaluation.score,
            cases_passed=evaluation.cases_passed,
            cases_total=evaluation.cases_total,
            equivalent=evaluation.equivalent,
            holdout_passed=evaluation.holdout_passed,
            holdout_total=len(evaluation.holdout_cases),
            holdout_equivalent=evaluation.holdout_equivalent,
            static_valid=evaluation.static_valid,
            completed=evaluation.completed,
            cases=evaluation.cases,
            created_at=evaluation.created_at,
        )


class EvaluationSummaryResponse(BaseModel):
    routines: int
    equivalence_rate: float
    """Routines whose every case is equivalent / routines evaluated."""
    case_pass_rate: float
    holdout_equivalence_rate: float
    """The honest number: routines equivalent on cases never shown to the LLM."""
    holdout_case_pass_rate: float
    static_valid_rate: float
    """Executions whose code passed ast.parse (the pipeline's own blocking check)."""
    completion_rate: float
    """Executions that ended with code (success or partial)."""
    evaluations: tuple[EvaluationResponse, ...]

    @classmethod
    def from_domain(cls, summary: EvaluationSummary) -> EvaluationSummaryResponse:
        return cls(
            routines=len(summary.evaluations),
            equivalence_rate=summary.equivalence_rate,
            case_pass_rate=summary.case_pass_rate,
            holdout_equivalence_rate=summary.holdout_equivalence_rate,
            holdout_case_pass_rate=summary.holdout_case_pass_rate,
            static_valid_rate=summary.static_valid_rate,
            completion_rate=summary.completion_rate,
            evaluations=tuple(EvaluationResponse.from_domain(e) for e in summary.evaluations),
        )
