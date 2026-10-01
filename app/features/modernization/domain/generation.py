from app.features.modernization.domain.enums import GenerationStrategy
from app.shared.domain.value_object import ValueObject


class ArchitecturalDecision(ValueObject):
    topic: str
    decision: str
    rationale: str


class RepairFeedback(ValueObject):
    """What the previous attempt produced and why validation rejected it."""

    attempt: int
    """The attempt being requested (2 = first retry)."""
    previous_code: str
    issues: tuple[str, ...]


class GenerationResult(ValueObject):
    """Everything about one generation except the code (returned next to it)."""

    strategy: GenerationStrategy
    recommended_strategy: GenerationStrategy
    architectural_decisions: tuple[ArchitecturalDecision, ...] = ()
    warnings: tuple[str, ...] = ()
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None
    attempt: int = 1
    """1 = first generation; >1 = regenerated after failing validation."""
