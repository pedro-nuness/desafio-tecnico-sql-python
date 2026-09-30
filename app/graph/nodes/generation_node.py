from app.application.services.code_generation_service import CodeGenerationService
from app.domain.enums import PipelineStep
from app.domain.exceptions import GenerationError, ModernizationError
from app.graph.state import ModernizationState, StateUpdate, failed


class GenerationNode:
    """Delegates to CodeGenerationService; knows nothing about prompts or vendors."""

    def __init__(self, generation_service: CodeGenerationService) -> None:
        self._generation_service = generation_service

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = state.get("parsed_procedure")
        analysis = state.get("semantic_analysis")
        if procedure is None or analysis is None:
            return failed(
                PipelineStep.GENERATION, ModernizationError("generation requires parse + analysis")
            )
        try:
            result = await self._generation_service.generate(
                procedure=procedure,
                analysis=analysis,
                source_code=state["source_code"],
                schema_context=state.get("schema_context"),
            )
        except GenerationError as exc:
            return failed(PipelineStep.GENERATION, exc)
        return StateUpdate(
            generation=result,
            generated_code=result.code,
            completed_steps=[PipelineStep.GENERATION],
        )
