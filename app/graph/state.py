import operator
from typing import Annotated, TypedDict
from uuid import UUID

from app.domain.enums import ModernizationStatus, PipelineStep
from app.domain.models.generation import GenerationResult
from app.domain.models.modernization import PipelineError
from app.domain.models.parsing import ParsedProcedure
from app.domain.models.semantic_analysis import SemanticAnalysis
from app.domain.models.validation import ValidationResult


class ModernizationState(TypedDict):
    execution_id: UUID
    source_code: str
    schema_context: str | None

    parsed_procedure: ParsedProcedure | None
    semantic_analysis: SemanticAnalysis | None

    generated_code: str | None
    generation: GenerationResult | None
    """Code + strategy + architectural decisions + GenerationMetadata."""

    validation_result: ValidationResult | None

    # Append-only channels: each node contributes, nothing is overwritten.
    completed_steps: Annotated[list[PipelineStep], operator.add]
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[PipelineError], operator.add]

    status: ModernizationStatus


class StateUpdate(TypedDict, total=False):
    """Partial update returned by a node (LangGraph merges it into the state)."""

    parsed_procedure: ParsedProcedure
    semantic_analysis: SemanticAnalysis
    generated_code: str
    generation: GenerationResult
    validation_result: ValidationResult
    completed_steps: list[PipelineStep]
    warnings: list[str]
    errors: list[PipelineError]
    status: ModernizationStatus


def initial_state(
    *, execution_id: UUID, source_code: str, schema_context: str | None
) -> ModernizationState:
    return ModernizationState(
        execution_id=execution_id,
        source_code=source_code,
        schema_context=schema_context,
        parsed_procedure=None,
        semantic_analysis=None,
        generated_code=None,
        generation=None,
        validation_result=None,
        completed_steps=[],
        warnings=[],
        errors=[],
        status=ModernizationStatus.RUNNING,
    )


def failed(step: PipelineStep, exc: Exception) -> StateUpdate:
    return StateUpdate(
        errors=[PipelineError(step=step, error_type=type(exc).__name__, message=str(exc))],
        status=ModernizationStatus.FAILURE,
    )
