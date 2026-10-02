"""One execution of the pipeline: the Modernization aggregate, its status and its
structured (JSONB-ready) report."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid7

from pydantic import Field, JsonValue

from app.features.modernization.analysis.domain import SemanticAnalysis
from app.features.modernization.case_generation.domain import CaseGenerationResult
from app.features.modernization.generation.domain import GenerationResult
from app.features.modernization.parsing.domain import Parameter, ParsedProcedure, RoutineKind
from app.features.modernization.validation.domain import ValidationResult
from app.shared.domain.value_object import ValueObject
from app.shared.errors import AppError


class ModernizationStatus(StrEnum):
    """Lifecycle of a modernization execution.

    RUNNING  -> persisted before the pipeline starts (a crash leaves a visible trace).
    SUCCESS  -> code generated and every validator passed.
    PARTIAL  -> code generated and syntactically valid, but non-blocking validators
                reported issues or a late step failed.
    FAILURE  -> no usable code (an early step failed or the code is not valid Python).
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


class PipelineStep(StrEnum):
    PARSING = "parsing"
    SEMANTIC_ANALYSIS = "semantic_analysis"
    GENERATION = "generation"
    CASE_GENERATION = "case_generation"
    """Runs in parallel with GENERATION (fan-out after the analysis)."""
    VALIDATION = "validation"


class PipelineError(ValueObject):
    step: PipelineStep | None
    """None when the failure happened outside a known step (orchestration crash)."""
    error_type: str
    message: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    """Structured data of an AppError (same payload the HTTP error response carries)."""

    @classmethod
    def from_exception(cls, step: PipelineStep | None, exc: Exception) -> PipelineError:
        return cls(
            step=step,
            error_type=type(exc).__name__,
            message=str(exc),
            payload=exc.payload if isinstance(exc, AppError) else {},
        )


@dataclass(slots=True)
class PipelineProgress:
    """Per-request channel: lets the global handler point to the recorded execution."""

    execution_id: UUID | None = None


# --------------------------------------------------------------------------- report


class ParsingSummary(ValueObject):
    """What the report keeps of the parsed routine (the full IR is too big for the JSONB)."""

    procedure_name: str | None = None
    kind: RoutineKind | None = None
    parser: str | None = None
    parameters: tuple[Parameter, ...] = ()
    return_type: str | None = None
    statement_count: int = 0
    referenced_tables: tuple[str, ...] = ()
    called_functions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def of(cls, procedure: ParsedProcedure) -> ParsingSummary:
        return cls(
            procedure_name=procedure.qualified_name,
            kind=procedure.kind,
            parser=procedure.parser,
            parameters=procedure.parameters,
            return_type=procedure.return_type,
            statement_count=sum(1 for _ in procedure.iter_statements()),
            referenced_tables=procedure.referenced_tables,
            called_functions=procedure.called_functions,
            warnings=procedure.warnings,
        )


class ModernizationReport(ValueObject):
    """What each step produced, even when the run stopped early. Stored as JSONB.

    A step that did not run (or failed) leaves its field None; `errors` says why.
    """

    parsing: ParsingSummary | None = None
    semantic_analysis: SemanticAnalysis | None = None
    generation: GenerationResult | None = None
    """The latest attempt (the code itself lives in Modernization.generated_code)."""
    case_generation: CaseGenerationResult | None = None
    """None when the step did not run (disabled, no schema, no evaluation database)."""
    validation: ValidationResult | None = None
    completed_steps: tuple[PipelineStep, ...] = ()
    errors: tuple[PipelineError, ...] = ()
    warnings: tuple[str, ...] = ()

    def status(self) -> ModernizationStatus:
        if self.generation is None:
            return ModernizationStatus.FAILURE
        if self.validation is None:
            return ModernizationStatus.PARTIAL  # code exists but was never verified
        if not self.validation.is_valid:
            return ModernizationStatus.FAILURE
        if self.errors or not self.validation.passed_all:
            return ModernizationStatus.PARTIAL
        return ModernizationStatus.SUCCESS


# --------------------------------------------------------------------------- aggregate


class Modernization(ValueObject):
    """One execution of the modernization pipeline (aggregate root)."""

    id: UUID
    source_code: str
    schema_context: str | None = None
    status: ModernizationStatus
    generated_code: str | None = None
    report: ModernizationReport = ModernizationReport()
    created_at: datetime
    updated_at: datetime

    @classmethod
    def start(
        cls,
        source_code: str,
        schema_context: str | None = None,
        *,
        now: datetime | None = None,
    ) -> Modernization:
        timestamp = now or datetime.now(UTC)
        return cls(
            id=uuid7(),
            source_code=source_code,
            schema_context=schema_context,
            status=ModernizationStatus.RUNNING,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def complete(
        self,
        report: ModernizationReport,
        generated_code: str | None,
        *,
        now: datetime | None = None,
    ) -> Modernization:
        return self.model_copy(
            update={
                "status": report.status(),
                "generated_code": generated_code,
                "report": report,
                "updated_at": now or datetime.now(UTC),
            }
        )

    def fail(
        self,
        report: ModernizationReport,
        generated_code: str | None,
        error: PipelineError,
        *,
        now: datetime | None = None,
    ) -> Modernization:
        """Interrupted by an exception: keeps everything produced so far, always FAILURE."""
        interrupted = report.model_copy(update={"errors": (*report.errors, error)})
        return self.complete(interrupted, generated_code, now=now).model_copy(
            update={"status": ModernizationStatus.FAILURE}
        )
