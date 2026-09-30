from app.application.ports.validation.code_validator import CodeValidator
from app.domain.enums import PipelineStep
from app.domain.exceptions import ModernizationError, ValidationExecutionError
from app.graph.state import ModernizationState, StateUpdate, failed


class ValidationNode:
    """Runs the configured CodeValidator (usually a composite) on the generated code."""

    def __init__(self, validator: CodeValidator) -> None:
        self._validator = validator

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        code = state.get("generated_code")
        if code is None:
            return failed(
                PipelineStep.VALIDATION, ModernizationError("no generated code to validate")
            )
        try:
            result = await self._validator.validate(code)
        except ValidationExecutionError as exc:
            return failed(PipelineStep.VALIDATION, exc)
        return StateUpdate(validation_result=result, completed_steps=[PipelineStep.VALIDATION])
