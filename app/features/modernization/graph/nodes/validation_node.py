from app.features.modernization.application.ports.validation.code_validator import (
    CodeValidator,
)
from app.features.modernization.domain.enums import PipelineStep
from app.features.modernization.graph.state import (
    ModernizationState,
    StateUpdate,
)
from app.shared.errors import AppError


class ValidationNode:
    """Runs the configured CodeValidator (usually a composite) on the generated code."""

    def __init__(self, validator: CodeValidator) -> None:
        self._validator = validator

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        code = state.get("generated_code")
        if code is None:
            raise AppError("no generated code to validate")
        result = await self._validator.validate(code)
        return StateUpdate(validation_result=result, completed_steps=[PipelineStep.VALIDATION])
