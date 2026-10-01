from collections.abc import Callable

import pytest

from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.generation import GenerationResult
from app.features.modernization.domain.parsing import ParsedProcedure
from app.features.modernization.domain.semantic_analysis import SemanticAnalysis
from app.features.modernization.domain.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.generation.generate_code import GenerateCode
from app.features.modernization.generation.prompt import (
    PROMPT_VERSION,
    GenerationPromptBuilder,
)
from app.features.modernization.parsing.plpgsql import PglastParser
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse, ResponseFormat
from tests.conftest import VALID_CODE, llm_payload
from tests.fakes import FakeLLM

type Analyzed = tuple[str, ParsedProcedure, SemanticAnalysis]


@pytest.fixture
def analyzed(load_procedure: Callable[[str], str]) -> Analyzed:
    source = load_procedure("process_orders")
    procedure = PglastParser().parse(source)
    return source, procedure, SemanticAnalyzer().analyze(procedure)


async def _generate(
    llm: FakeLLM, analyzed: Analyzed, schema: str | None = None
) -> GenerationResult:
    source, procedure, analysis = analyzed
    step = GenerateCode(llm, GenerationPromptBuilder())
    return await step.execute(
        procedure=procedure, analysis=analysis, source_code=source, schema_context=schema
    )


async def test_generates_code_with_metadata(analyzed: Analyzed) -> None:
    llm = FakeLLM([llm_payload(strategy="database_delegated")])

    result = await _generate(llm, analyzed)

    assert result.code == VALID_CODE
    assert result.strategy is GenerationStrategy.DATABASE_DELEGATED
    assert result.recommended_strategy is GenerationStrategy.HYBRID
    assert result.metadata.provider == "fake"
    assert result.metadata.prompt_version == PROMPT_VERSION
    assert result.architectural_decisions[0].topic == "sql"


async def test_prompt_carries_parsing_and_semantic_analysis(analyzed: Analyzed) -> None:
    llm = FakeLLM([llm_payload()])

    await _generate(llm, analyzed, schema="CREATE TABLE orders(id int);")

    [request] = llm.requests
    assert request.response_format is ResponseFormat.JSON
    prompt = request.user_prompt
    # Parsing output, not just the raw procedure
    assert "billing.process_customer_orders" in prompt
    assert "Control-flow outline" in prompt and "FOR_QUERY" in prompt
    assert "locking=FOR UPDATE" in prompt
    # Semantic analysis output
    assert "N_PLUS_ONE" in prompt and "recommended strategy: hybrid" in prompt
    assert "CREATE TABLE orders" in prompt
    assert "keep relational logic close to the database" in request.system_prompt


async def test_accepts_json_wrapped_in_markdown_fences(analyzed: Analyzed) -> None:
    llm = FakeLLM([f"Here you go:\n```json\n{llm_payload()}\n```"])

    result = await _generate(llm, analyzed)

    assert result.code == VALID_CODE


@pytest.mark.parametrize(
    ("content", "error_type"),
    [
        ("no json here", IntegrationError),
        ('{"strategy": "hybrid"}', IntegrationError),  # missing code
        ('{"python_code": "   ", "strategy": "hybrid"}', IntegrationError),  # empty code
        (
            '{"python_code": "x = 1", "strategy": "rewrite_everything"}',
            IntegrationError,
        ),  # unknown strategy
    ],
)
async def test_invalid_llm_answers_propagate_errors(
    analyzed: Analyzed, content: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        await _generate(FakeLLM([content]), analyzed)


async def test_missing_strategy_falls_back_to_recommendation(analyzed: Analyzed) -> None:
    result = await _generate(FakeLLM(['{"python_code": "x = 1"}']), analyzed)

    assert result.strategy is GenerationStrategy.HYBRID
    assert any("did not report a strategy" in warning for warning in result.warnings)


async def test_truncated_answer_reports_token_limit(analyzed: Analyzed) -> None:
    class TruncatingLLM(FakeLLM):
        async def generate(self, request: LLMRequest) -> LLMResponse:
            response = await super().generate(request)
            # Reasoning model that spent the whole budget thinking: no content at all.
            return response.model_copy(update={"content": "", "finish_reason": "length"})

    with pytest.raises(IntegrationError, match="LLM_MAX_OUTPUT_TOKENS"):
        await _generate(TruncatingLLM(), analyzed)


async def test_provider_errors_propagate_unchanged(analyzed: Analyzed) -> None:
    error = IntegrationError("rate limited")
    with pytest.raises(IntegrationError) as exc_info:
        await _generate(FakeLLM(error=error), analyzed)
    assert exc_info.value is error


async def test_route_failover_is_reported_as_a_warning(analyzed: Analyzed) -> None:
    class FailedOverLLM(FakeLLM):
        async def generate(self, request: LLMRequest) -> LLMResponse:
            response = await super().generate(request)
            return response.model_copy(
                update={"failed_routes": ("openrouter/z-ai/glm-5.3-flash: openrouter timed out",)}
            )

    result = await _generate(FailedOverLLM(provider="openai", model="gpt-5-mini"), analyzed)

    assert (
        "LLM route openrouter/z-ai/glm-5.3-flash: openrouter timed out; "
        "answered by openai/gpt-5-mini"
    ) in result.warnings
    assert (result.metadata.provider, result.metadata.model) == ("openai", "gpt-5-mini")
