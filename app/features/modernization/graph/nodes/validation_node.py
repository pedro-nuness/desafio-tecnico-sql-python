from app.features.modernization.application.ports.validation.code_validator import (
    CodeValidator,
)
from app.features.modernization.domain.enums import PipelineStep
from app.features.modernization.domain.exceptions import (
    ModernizationError,
)
from app.features.modernization.graph.state import (
    ModernizationState,
    StateUpdate,
)


class ValidationNode:
    """Runs the configured CodeValidator (usually a composite) on the generated code."""

    def __init__(self, validator: CodeValidator) -> None:
        self._validator = validator

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        code = state.get("generated_code")
        if code is None:
            raise ModernizationError("no generated code to validate")
        result = await self._validator.validate(code)
        return StateUpdate(validation_result=result, completed_steps=[PipelineStep.VALIDATION])
