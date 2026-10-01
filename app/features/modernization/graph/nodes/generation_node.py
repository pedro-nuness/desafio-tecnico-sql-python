from app.features.modernization.application.services.code_generation_service import (
    CodeGenerationService,
)
from app.features.modernization.domain.enums import PipelineStep
from app.features.modernization.domain.exceptions import (
    GenerationError,
    ModernizationError,
)
from app.features.modernization.domain.models.generation import RepairFeedback
from app.features.modernization.graph.state import (
    ModernizationState,
    StateUpdate,
    failed,
)


class GenerationNode:
    """Delegates to CodeGenerationService; knows nothing about prompts or vendors.

    On a retry (routed back from validation) the previous code and the validation
    issues go into the prompt as RepairFeedback.
    """

    def __init__(self, generation_service: CodeGenerationService) -> None:
        self._generation_service = generation_service

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = state.get("parsed_procedure")
        analysis = state.get("semantic_analysis")
        if procedure is None or analysis is None:
            return failed(
                PipelineStep.GENERATION, ModernizationError("generation requires parse + analysis")
            )
        attempt = state.get("generation_attempts", 0) + 1
        feedback = _feedback(state, attempt)
        try:
            result = await self._generation_service.generate(
                procedure=procedure,
                analysis=analysis,
                source_code=state["source_code"],
                schema_context=state.get("schema_context"),
                feedback=feedback,
            )
        except GenerationError as exc:
            # Earlier attempts (if any) stay in the state: the report keeps the best so far.
            return failed(PipelineStep.GENERATION, exc) | StateUpdate(generation_attempts=attempt)
        warnings = []
        if feedback is not None:
            warnings.append(
                f"generation attempt {attempt}: regenerated after {len(feedback.issues)} "
                "validation issue(s) in the previous attempt"
            )
        return StateUpdate(
            generation=result,
            generated_code=result.code,
            generation_attempts=attempt,
            completed_steps=[PipelineStep.GENERATION],
            warnings=warnings,
        )


def _feedback(state: ModernizationState, attempt: int) -> RepairFeedback | None:
    previous_code = state.get("generated_code")
    validation = state.get("validation_result")
    if attempt == 1 or previous_code is None or validation is None:
        return None
    return RepairFeedback(attempt=attempt, previous_code=previous_code, issues=validation.issues())
