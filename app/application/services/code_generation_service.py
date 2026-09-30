import json

from pydantic import BaseModel, ValidationError

from app.application.ports.llm.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ResponseFormat,
)
from app.domain.enums import GenerationStrategy
from app.domain.exceptions import GenerationError
from app.domain.models.generation import (
    ArchitecturalDecision,
    GenerationMetadata,
    GenerationResult,
)
from app.domain.models.parsing import ParsedProcedure
from app.domain.models.semantic_analysis import SemanticAnalysis
from app.prompts.generation_prompt import GenerationPromptBuilder


class _DecisionPayload(BaseModel):
    topic: str
    decision: str
    rationale: str = ""


class _GenerationPayload(BaseModel):
    """Contract the LLM must answer with (see SYSTEM_PROMPT). Extra keys are ignored."""

    python_code: str
    strategy: GenerationStrategy | None = None
    """Missing -> the deterministic recommendation is used (and a warning recorded)."""
    architectural_decisions: list[_DecisionPayload] = []
    warnings: list[str] = []


class CodeGenerationService:
    def __init__(
        self,
        llm: LLMProvider,
        prompt_builder: GenerationPromptBuilder,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> None:
        self._llm = llm
        self._prompt_builder = prompt_builder
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def generate(
        self,
        *,
        procedure: ParsedProcedure,
        analysis: SemanticAnalysis,
        source_code: str,
        schema_context: str | None,
    ) -> GenerationResult:
        prompt = self._prompt_builder.build(
            procedure=procedure,
            analysis=analysis,
            source_code=source_code,
            schema_context=schema_context,
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
        try:
            payload = parse_generation_payload(response)
        except GenerationError as exc:
            if response.finish_reason == "length":
                raise GenerationError(
                    f"LLM hit the output token limit ({self._max_output_tokens}) before "
                    "finishing the answer (reasoning models count reasoning tokens too): "
                    "raise LLM_MAX_OUTPUT_TOKENS or lower LLM_REASONING_EFFORT"
                ) from exc
            raise
        if not payload.python_code.strip():
            raise GenerationError("LLM returned empty python_code")

        warnings = list(payload.warnings)
        if response.finish_reason == "length":
            warnings.append("LLM output hit the token limit; the module may be truncated.")
        if payload.strategy is None:
            warnings.append(
                "LLM did not report a strategy; assuming the recommended one "
                f"({analysis.recommended_strategy.value})."
            )
        return GenerationResult(
            code=payload.python_code,
            strategy=payload.strategy or analysis.recommended_strategy,
            recommended_strategy=analysis.recommended_strategy,
            architectural_decisions=tuple(
                ArchitecturalDecision(**decision.model_dump())
                for decision in payload.architectural_decisions
            ),
            warnings=tuple(warnings),
            metadata=GenerationMetadata(
                provider=response.provider,
                model=response.model,
                prompt_version=prompt.version,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                finish_reason=response.finish_reason,
            ),
        )


def parse_generation_payload(response: LLMResponse) -> _GenerationPayload:
    """Accept a bare JSON object, optionally wrapped in prose or Markdown fences."""
    content = response.content
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise GenerationError("LLM response does not contain a JSON object")
    try:
        return _GenerationPayload.model_validate(json.loads(content[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GenerationError(f"LLM response does not match the expected contract: {exc}") from exc
