"""What the case generation step produces, next to the scenario it hands to validation."""

from app.shared.domain.value_object import ValueObject


class CaseGenerationResult(ValueObject):
    """Which proposed cases were kept or discarded, and the LLM call that proposed them."""

    seed_generated: bool
    """True when the LLM also wrote the seed (the caller sent no `behavior`)."""
    kept: tuple[str, ...] = ()
    discarded: tuple[str, ...] = ()
    """`<case>: <reason>` for every proposed case left out."""
    warnings: tuple[str, ...] = ()
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
