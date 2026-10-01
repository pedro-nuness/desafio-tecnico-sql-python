import json
import re

from pydantic import BaseModel, ValidationError

from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.generation import (
    ArchitecturalDecision,
    GenerationMetadata,
    GenerationResult,
    RepairFeedback,
)
from app.features.modernization.domain.parsing import ParsedProcedure
from app.features.modernization.domain.semantic_analysis import SemanticAnalysis
from app.features.modernization.generation.prompt import GenerationPromptBuilder
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.llm import LLM, LLMRequest, LLMResponse, ResponseFormat


class _DecisionPayload(BaseModel):
    topic: str
    decision: str
    rationale: str = ""


class _GenerationPayload(BaseModel):
    """Contract the LLM must answer with (see SYSTEM_PROMPT). Extra keys are ignored."""

    python_code: str
    strategy: GenerationStrategy | None = None
    """Missing -> the deterministic recommendation is used (and a warning recorded)."""
    architectural_decisions: list[object] = []
    """Validated one by one: a malformed decision is dropped, not the whole answer."""
    warnings: list[str] = []


class GenerateCode:
    """Generation step: prompt from the deterministic analysis -> LLM -> GenerationResult."""

    def __init__(
        self,
        llm: LLM,
        prompt_builder: GenerationPromptBuilder,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> None:
        self._llm = llm
        self._prompt_builder = prompt_builder
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def execute(
        self,
        *,
        procedure: ParsedProcedure,
        analysis: SemanticAnalysis,
        source_code: str,
        schema_context: str | None,
        feedback: RepairFeedback | None = None,
    ) -> GenerationResult:
        prompt = self._prompt_builder.build(
            procedure=procedure,
            analysis=analysis,
            source_code=source_code,
            schema_context=schema_context,
            feedback=feedback,
        )
        response = await self._llm.generate(
            LLMRequest(
                system_prompt=prompt.system,
                user_prompt=prompt.user,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
                response_format=ResponseFormat.JSON,
            )
        )
        # Needed: the LLM answering outside the contract is an integration failure, not a
        # raw JSON/pydantic error; a truncated answer says so explicitly (AD-04).
        try:
            payload = parse_generation_payload(response)
        except ValueError as exc:  # JSONDecodeError and pydantic's ValidationError included
            message = (
                f"LLM hit the output token limit ({self._max_output_tokens}) before finishing "
                "the answer (reasoning models count reasoning tokens too): raise "
                "LLM_MAX_OUTPUT_TOKENS or lower LLM_REASONING_EFFORT"
                if response.finish_reason == "length"
                else f"LLM response does not match the expected contract: {exc}"
            )
            raise IntegrationError(message, finish_reason=response.finish_reason) from exc
        code = strip_code_fence(payload.python_code)
        if not code.strip():
            raise IntegrationError(
                "LLM returned empty python_code", finish_reason=response.finish_reason
            )

        warnings = list(payload.warnings)
        if code != payload.python_code:
            warnings.append("Removed a Markdown fence the LLM left inside python_code.")
        decisions = _decisions(payload.architectural_decisions)
        if dropped := len(payload.architectural_decisions) - len(decisions):
            warnings.append(
                f"Dropped {dropped} architectural decision(s) outside the contract "
                "(topic, decision, rationale)."
            )
        warnings.extend(
            f"LLM route {failure}; answered by {response.provider}/{response.model}"
            for failure in response.failed_routes
        )
        if response.finish_reason == "length":
            warnings.append("LLM output hit the token limit; the module may be truncated.")
        if payload.strategy is None:
            warnings.append(
                "LLM did not report a strategy; assuming the recommended one "
                f"({analysis.recommended_strategy.value})."
            )
        return GenerationResult(
            code=code,
            strategy=payload.strategy or analysis.recommended_strategy,
            recommended_strategy=analysis.recommended_strategy,
            architectural_decisions=decisions,
            warnings=tuple(warnings),
            metadata=GenerationMetadata(
                provider=response.provider,
                model=response.model,
                prompt_version=prompt.version,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                finish_reason=response.finish_reason,
                attempt=feedback.attempt if feedback else 1,
            ),
        )


def parse_generation_payload(response: LLMResponse) -> _GenerationPayload:
    """Accept a bare JSON object, optionally wrapped in prose or Markdown fences."""
    content = response.content
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("LLM response does not contain a JSON object")
    return _GenerationPayload.model_validate(json.loads(content[start : end + 1]))


def _decisions(items: list[object]) -> tuple[ArchitecturalDecision, ...]:
    decisions: list[ArchitecturalDecision] = []
    for item in items:
        # Needed: decisions are report metadata; one off-contract entry (observed:
        # {"transaction": "..."} without topic) must not discard valid generated code.
        try:
            decision = _DecisionPayload.model_validate(item)
        except ValidationError:
            continue
        decisions.append(ArchitecturalDecision(**decision.model_dump()))
    return tuple(decisions)


_OPENING_FENCE = re.compile(r"(```\s*)?(python|py)?\s*", re.IGNORECASE)


def strip_code_fence(code: str) -> str:
    """Drop a Markdown fence left inside python_code, whole or in part.

    Observed with glm-5.3-flash: a full ```python ... ``` fence, a bare "python" first line,
    and a closing ``` glued to the last line. A bare "python" line is never meaningful module
    code. Anything else is returned unchanged.
    """
    lines = code.strip().splitlines()
    stripped = False
    if lines and _OPENING_FENCE.fullmatch(lines[0].strip()):
        lines, stripped = lines[1:], True
    if lines and lines[-1].rstrip().endswith("```"):
        lines[-1] = lines[-1].rstrip().removesuffix("```")
        if not lines[-1].strip():
            lines.pop()
        stripped = True
    return "\n".join(lines).rstrip() + "\n" if stripped else code
