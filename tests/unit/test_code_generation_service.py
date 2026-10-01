from collections.abc import Callable

import pytest

from app.features.modernization.application.ports.llm.llm_provider import (
    LLMRequest,
    LLMResponse,
    ResponseFormat,
)
from app.features.modernization.application.services.code_generation_service import (
    CodeGenerationService,
)
from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.exceptions import GenerationError, LLMProviderError
from app.features.modernization.domain.models.generation import GenerationResult
from app.features.modernization.domain.models.parsing import ParsedProcedure
from app.features.modernization.domain.models.semantic_analysis import SemanticAnalysis
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.infrastructure.parsing.pglast_parser import PglastParser
from app.features.modernization.prompts.generation_prompt import (
    PROMPT_VERSION,
    GenerationPromptBuilder,
)
from tests.conftest import VALID_CODE, llm_payload
from tests.fakes import FakeLLMProvider

type Analyzed = tuple[str, ParsedProcedure, SemanticAnalysis]


@pytest.fixture
def analyzed(load_procedure: Callable[[str], str]) -> Analyzed:
    source = load_procedure("process_orders")
    procedure = PglastParser().parse(source)
    return source, procedure, SemanticAnalyzer().analyze(procedure)


async def _generate(
    llm: FakeLLMProvider, analyzed: Analyzed, schema: str | None = None
) -> GenerationResult:
    source, procedure, analysis = analyzed
    service = CodeGenerationService(llm, GenerationPromptBuilder())
    return await service.generate(
        procedure=procedure, analysis=analysis, source_code=source, schema_context=schema
    )


async def test_generates_code_with_metadata(analyzed: Analyzed) -> None:
    llm = FakeLLMProvider([llm_payload(strategy="database_delegated")])

    result = await _generate(llm, analyzed)

    assert result.code == VALID_CODE
    assert result.strategy is GenerationStrategy.DATABASE_DELEGATED
    assert result.recommended_strategy is GenerationStrategy.HYBRID
    assert result.metadata.provider == "fake"
    assert result.metadata.prompt_version == PROMPT_VERSION
    assert result.architectural_decisions[0].topic == "sql"


async def test_prompt_carries_parsing_and_semantic_analysis(analyzed: Analyzed) -> None:
    llm = FakeLLMProvider([llm_payload()])

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
    llm = FakeLLMProvider([f"Here you go:\n```json\n{llm_payload()}\n```"])

    result = await _generate(llm, analyzed)

    assert result.code == VALID_CODE


@pytest.mark.parametrize(
    "content",
    [
        "no json here",
        '{"strategy": "hybrid"}',  # missing code
        '{"python_code": "   ", "strategy": "hybrid"}',  # empty code
        '{"python_code": "x = 1", "strategy": "rewrite_everything"}',  # unknown strategy
    ],
)
async def test_invalid_llm_answers_raise_generation_error(analyzed: Analyzed, content: str) -> None:
    with pytest.raises(GenerationError):
        await _generate(FakeLLMProvider([content]), analyzed)


async def test_missing_strategy_falls_back_to_recommendation(analyzed: Analyzed) -> None:
    result = await _generate(FakeLLMProvider(['{"python_code": "x = 1"}']), analyzed)

    assert result.strategy is GenerationStrategy.HYBRID
    assert any("did not report a strategy" in warning for warning in result.warnings)


async def test_truncated_answer_reports_token_limit(analyzed: Analyzed) -> None:
    class TruncatingLLM(FakeLLMProvider):
        async def generate(self, request: LLMRequest) -> LLMResponse:
            response = await super().generate(request)
            # Reasoning model that spent the whole budget thinking: no content at all.
            return response.model_copy(update={"content": "", "finish_reason": "length"})

    with pytest.raises(GenerationError, match="LLM_MAX_OUTPUT_TOKENS"):
        await _generate(TruncatingLLM(), analyzed)


async def test_provider_errors_propagate_as_domain_errors(analyzed: Analyzed) -> None:
    llm = FakeLLMProvider(error=LLMProviderError("rate limited"))

    with pytest.raises(LLMProviderError, match="rate limited"):
        await _generate(llm, analyzed)
