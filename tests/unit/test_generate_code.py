import json
from collections.abc import Callable

import pytest

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.analysis.domain import GenerationStrategy, SemanticAnalysis
from app.features.modernization.code_generation.domain import CodeGenerationResult
from app.features.modernization.code_generation.generate_code import GenerateCode
from app.features.modernization.code_generation.prompt import (
    PROMPT_VERSION,
    CodeGenerationPromptBuilder,
)
from app.features.modernization.parsing.domain import ParsedProcedure
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
) -> tuple[str, CodeGenerationResult]:
    source, procedure, analysis = analyzed
    step = GenerateCode(llm, CodeGenerationPromptBuilder())
    return await step.execute(
        procedure=procedure, analysis=analysis, source_code=source, schema_context=schema
    )


async def test_generates_code_with_metadata(analyzed: Analyzed) -> None:
    llm = FakeLLM([llm_payload(strategy="database_delegated")])

    code, result = await _generate(llm, analyzed)

    assert code == VALID_CODE
    assert result.strategy is GenerationStrategy.DATABASE_DELEGATED
    assert result.recommended_strategy is GenerationStrategy.HYBRID
    assert result.provider == "fake"
    assert result.prompt_version == PROMPT_VERSION
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

    code, _ = await _generate(llm, analyzed)

    assert code == VALID_CODE


@pytest.mark.parametrize(
    "fenced",
    [
        f"```python\n{VALID_CODE}```",
        f"python\n{VALID_CODE}",  # observed: run v3 #5, annex F
        f"python\n{VALID_CODE.rstrip()}```",  # observed: run v3 #4, annex D
        f"```\n{VALID_CODE}\n```\n",
    ],
)
async def test_markdown_fence_inside_python_code_is_removed(
    analyzed: Analyzed, fenced: str
) -> None:
    code, result = await _generate(FakeLLM([llm_payload(code=fenced)]), analyzed)

    assert code == VALID_CODE
    assert "Removed a Markdown fence the LLM left inside python_code." in result.warnings


async def test_off_contract_decisions_are_dropped_not_the_code(analyzed: Analyzed) -> None:
    content = json.dumps(
        {
            "python_code": "x = 1",
            "strategy": "hybrid",
            "architectural_decisions": [
                {"topic": "sql", "decision": "kept in SQL"},
                {"transaction": "Caller owns the transaction."},  # observed: prompt v4 run
            ],
        }
    )

    code, result = await _generate(FakeLLM([content]), analyzed)

    assert code == "x = 1"
    assert [d.topic for d in result.architectural_decisions] == ["sql"]
    assert any("Dropped 1 architectural decision(s)" in w for w in result.warnings)


async def test_unfenced_code_is_kept_verbatim(analyzed: Analyzed) -> None:
    code, result = await _generate(FakeLLM([llm_payload(code="x = 1")]), analyzed)

    assert code == "x = 1"
    assert not any("Markdown fence" in warning for warning in result.warnings)


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
    _, result = await _generate(FakeLLM(['{"python_code": "x = 1"}']), analyzed)

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

    _, result = await _generate(FailedOverLLM(provider="openai", model="gpt-5-mini"), analyzed)

    assert (
        "LLM route openrouter/z-ai/glm-5.3-flash: openrouter timed out; "
        "answered by openai/gpt-5-mini"
    ) in result.warnings
    assert (result.provider, result.model) == ("openai", "gpt-5-mini")
