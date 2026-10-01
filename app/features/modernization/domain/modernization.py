"""Modernization aggregate, pipeline outcome and the structured (JSONB-ready) report."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid7

from pydantic import Field, JsonValue

from app.features.modernization.domain.enums import (
    GenerationStrategy,
    ModernizationStatus,
    PipelineStep,
)
from app.features.modernization.domain.generation import (
    ArchitecturalDecision,
    GenerationResult,
)
from app.features.modernization.domain.parsing import (
    Parameter,
    ParsedProcedure,
    RoutineKind,
)
from app.features.modernization.domain.semantic_analysis import (
    Dependency,
    DetectedFeature,
    Recommendation,
    SemanticAnalysis,
    SemanticRisk,
)
from app.features.modernization.domain.validation import (
    ValidationResult,
    ValidatorResult,
)
from app.shared.domain.value_object import ValueObject
from app.shared.errors import AppError


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


class PipelineOutcome(ValueObject):
    """Everything the pipeline managed to produce, even when it stopped early."""

    parsed_procedure: ParsedProcedure | None = None
    semantic_analysis: SemanticAnalysis | None = None
    generation: GenerationResult | None = None
    validation: ValidationResult | None = None
    completed_steps: tuple[PipelineStep, ...] = ()
    errors: tuple[PipelineError, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def generated_code(self) -> str | None:
        return self.generation.code if self.generation else None

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


@dataclass(slots=True)
class PipelineProgress:
    """Per-request channel: lets the global handler point to the recorded execution."""

    execution_id: UUID | None = None


# --------------------------------------------------------------------------- report


class ParsingReport(ValueObject):
    success: bool
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
    def from_procedure(cls, procedure: ParsedProcedure) -> ParsingReport:
        return cls(
            success=True,
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


class SemanticAnalysisReport(ValueObject):
    detected_features: tuple[DetectedFeature, ...] = ()
    risks: tuple[SemanticRisk, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    recommended_strategy: GenerationStrategy
    recommendations: tuple[Recommendation, ...] = ()

    @classmethod
    def from_analysis(cls, analysis: SemanticAnalysis) -> SemanticAnalysisReport:
        return cls(
            detected_features=analysis.features,
            risks=analysis.risks,
            dependencies=analysis.dependencies,
            recommended_strategy=analysis.recommended_strategy,
            recommendations=analysis.recommendations,
        )


class GenerationReport(ValueObject):
    success: bool
    strategy: GenerationStrategy | None = None
    recommended_strategy: GenerationStrategy | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    finish_reason: str | None = None
    attempt: int | None = None
    architectural_decisions: tuple[ArchitecturalDecision, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result: GenerationResult) -> GenerationReport:
        metadata = result.metadata
        return cls(
            success=True,
            strategy=result.strategy,
            recommended_strategy=result.recommended_strategy,
            provider=metadata.provider,
            model=metadata.model,
            prompt_version=metadata.prompt_version,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            latency_ms=metadata.latency_ms,
            finish_reason=metadata.finish_reason,
            attempt=metadata.attempt,
            architectural_decisions=result.architectural_decisions,
            warnings=result.warnings,
        )


class ValidationReport(ValueObject):
    valid_python: bool
    passed_all: bool
    validators: tuple[ValidatorResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result: ValidationResult) -> ValidationReport:
        warnings = tuple(
            f"[{validator.validator}] "
            + (f"L{message.line}: " if message.line else "")
            + (f"{message.code} " if message.code else "")
            + message.message
            for validator in result.results
            if not validator.success and not validator.blocking
            for message in validator.messages
        )
        return cls(
            valid_python=result.is_valid,
            passed_all=result.passed_all,
            validators=result.results,
            warnings=warnings,
        )


class ModernizationReport(ValueObject):
    parsing: ParsingReport | None = None
    semantic_analysis: SemanticAnalysisReport | None = None
    generation: GenerationReport | None = None
    validation: ValidationReport | None = None
    completed_steps: tuple[PipelineStep, ...] = ()
    errors: tuple[PipelineError, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_outcome(cls, outcome: PipelineOutcome) -> ModernizationReport:
        failed = {error.step for error in outcome.errors}
        return cls(
            parsing=(
                ParsingReport.from_procedure(outcome.parsed_procedure)
                if outcome.parsed_procedure
                else _failed_or_none(PipelineStep.PARSING in failed, ParsingReport)
            ),
            semantic_analysis=(
                SemanticAnalysisReport.from_analysis(outcome.semantic_analysis)
                if outcome.semantic_analysis
                else None
            ),
            generation=(
                GenerationReport.from_result(outcome.generation)
                if outcome.generation
                else _failed_or_none(PipelineStep.GENERATION in failed, GenerationReport)
            ),
            validation=(
                ValidationReport.from_result(outcome.validation) if outcome.validation else None
            ),
            completed_steps=outcome.completed_steps,
            errors=outcome.errors,
            warnings=outcome.warnings,
        )


def _failed_or_none[R: (ParsingReport, GenerationReport)](
    failed: bool, report: type[R]
) -> R | None:
    return report(success=False) if failed else None


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

    def complete(self, outcome: PipelineOutcome, *, now: datetime | None = None) -> Modernization:
        return self.model_copy(
            update={
                "status": outcome.status(),
                "generated_code": outcome.generated_code,
                "report": ModernizationReport.from_outcome(outcome),
                "updated_at": now or datetime.now(UTC),
            }
        )

    def fail(
        self, outcome: PipelineOutcome, error: PipelineError, *, now: datetime | None = None
    ) -> Modernization:
        """Interrupted by an exception: keeps everything produced so far, always FAILURE."""
        interrupted = outcome.model_copy(update={"errors": (*outcome.errors, error)})
        return self.complete(interrupted, now=now).model_copy(
            update={"status": ModernizationStatus.FAILURE}
        )
