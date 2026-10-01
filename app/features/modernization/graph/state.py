import operator
from datetime import datetime
from typing import Annotated, NotRequired, TypedDict
from uuid import UUID

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.generation import GenerationResult
from app.features.modernization.domain.modernization import (
    Modernization,
    PipelineError,
    PipelineOutcome,
)
from app.features.modernization.domain.parsing import ParsedProcedure
from app.features.modernization.domain.semantic_analysis import SemanticAnalysis
from app.features.modernization.domain.validation import ValidationResult


class ModernizationInput(TypedDict):
    """What a caller sends: POST /modernize, the LangGraph API (/runs) or Studio."""

    source_code: str
    schema_context: NotRequired[str | None]


class ModernizationState(TypedDict):
    source_code: str
    schema_context: NotRequired[str | None]

    # Set by record_start: every run has a persisted row from its first step.
    execution_id: UUID
    started_at: datetime

    parsed_procedure: ParsedProcedure | None
    semantic_analysis: SemanticAnalysis | None

    generated_code: str | None
    generation: GenerationResult | None
    """Code + strategy + architectural decisions + GenerationMetadata (latest attempt)."""
    generation_attempts: int

    validation_result: ValidationResult | None

    # Append-only channels: each node contributes, nothing is overwritten.
    completed_steps: Annotated[list[PipelineStep], operator.add]
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[PipelineError], operator.add]

    status: ModernizationStatus
    """RUNNING until record_result computes the final status."""

    modernization: Modernization
    """The persisted aggregate, set by record_result (the run's output)."""


class StateUpdate(TypedDict, total=False):
    """Partial update returned by a node (LangGraph merges it into the state)."""

    execution_id: UUID
    started_at: datetime
    parsed_procedure: ParsedProcedure
    semantic_analysis: SemanticAnalysis
    generated_code: str
    generation: GenerationResult
    generation_attempts: int
    validation_result: ValidationResult
    completed_steps: list[PipelineStep]
    warnings: list[str]
    errors: list[PipelineError]
    status: ModernizationStatus
    modernization: Modernization


def to_outcome(state: ModernizationState) -> PipelineOutcome:
    return PipelineOutcome(
        parsed_procedure=state.get("parsed_procedure"),
        semantic_analysis=state.get("semantic_analysis"),
        generation=state.get("generation"),
        validation=state.get("validation_result"),
        completed_steps=tuple(state.get("completed_steps", [])),
        errors=tuple(state.get("errors", [])),
        warnings=tuple(state.get("warnings", [])),
    )
