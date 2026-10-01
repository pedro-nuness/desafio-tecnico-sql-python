from uuid import UUID


class ModernizationError(Exception):
    """Base class for expected, domain-level failures of the pipeline."""


class ParsingError(ModernizationError):
    """The source could not be turned into a ParsedProcedure."""


class GenerationError(ModernizationError):
    """Code generation failed or produced an unusable answer."""


class LLMProviderError(GenerationError):
    """The LLM provider failed (network, auth, rate limit, timeout...)."""


class ValidationExecutionError(ModernizationError):
    """A validator could not run (as opposed to the code being invalid)."""


class ModernizationNotFoundError(ModernizationError):
    def __init__(self, modernization_id: UUID) -> None:
        super().__init__(f"Modernization {modernization_id} not found")
        self.modernization_id = modernization_id
