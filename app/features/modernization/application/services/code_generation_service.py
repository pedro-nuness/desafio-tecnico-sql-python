import json

from pydantic import BaseModel

from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.exceptions import GenerationError
from app.features.modernization.domain.models.generation import (
    ArchitecturalDecision,
    GenerationMetadata,
    GenerationResult,
    RepairFeedback,
)
from app.features.modernization.domain.models.parsing import ParsedProcedure
from app.features.modernization.domain.models.semantic_analysis import SemanticAnalysis
from app.features.modernization.prompts.generation_prompt import GenerationPromptBuilder
from app.shared.integrations.llm.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ResponseFormat,
)


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
        payload = parse_generation_payload(response)
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
                attempt=feedback.attempt if feedback else 1,
            ),
        )


def parse_generation_payload(response: LLMResponse) -> _GenerationPayload:
    """Accept a bare JSON object, optionally wrapped in prose or Markdown fences."""
    content = response.content
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise GenerationError("LLM response does not contain a JSON object")
    return _GenerationPayload.model_validate(json.loads(content[start : end + 1]))
