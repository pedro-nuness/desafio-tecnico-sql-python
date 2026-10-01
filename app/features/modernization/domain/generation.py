from app.features.modernization.domain.enums import GenerationStrategy
from app.shared.domain.value_object import ValueObject


class ArchitecturalDecision(ValueObject):
    topic: str
    decision: str
    rationale: str


class GenerationMetadata(ValueObject):
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None
    attempt: int = 1
    """1 = first generation; >1 = regenerated after failing validation."""


class RepairFeedback(ValueObject):
    """What the previous attempt produced and why validation rejected it."""

    attempt: int
    """The attempt being requested (2 = first retry)."""
    previous_code: str
    issues: tuple[str, ...]


class GenerationResult(ValueObject):
    code: str
    strategy: GenerationStrategy
    recommended_strategy: GenerationStrategy
    architectural_decisions: tuple[ArchitecturalDecision, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: GenerationMetadata
