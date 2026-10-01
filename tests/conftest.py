import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.features.modernization.application.ports.validation.code_validator import (
    CodeValidator,
)
from app.features.modernization.application.services.code_generation_service import (
    CodeGenerationService,
)
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.graph.builder import (
    DEFAULT_RETRY,
    ModernizationGraph,
    RetryPolicy,
    build_modernization_graph,
)
from app.features.modernization.graph.pipeline import LangGraphModernizationPipeline
from app.features.modernization.infrastructure.parsing.pglast_parser import PglastParser
from app.features.modernization.infrastructure.validation.composite_validator import (
    CompositeCodeValidator,
)
from app.features.modernization.infrastructure.validation.python_ast_validator import (
    PythonASTValidator,
)
from app.features.modernization.infrastructure.validation.ruff_validator import (
    RuffValidator,
)
from app.features.modernization.prompts.generation_prompt import (
    GenerationPromptBuilder,
)
from tests.fakes import FakeLLM, InMemoryDatabase, InMemoryModernizationRepository

PROCEDURES_DIR = Path(__file__).parent / "fixtures" / "procedures"

VALID_CODE = """\
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class Result:
    total_amount: float


async def process_customer_orders(conn: AsyncConnection, customer_id: int) -> Result:
    row = (
        await conn.execute(
            text("SELECT coalesce(sum(amount), 0) FROM orders WHERE customer_id = :cid"),
            {"cid": customer_id},
        )
    ).one()
    return Result(total_amount=float(row[0]))
"""


def llm_payload(code: str = VALID_CODE, strategy: str = "hybrid") -> str:
    return json.dumps(
        {
            "python_code": code,
            "strategy": strategy,
            "architectural_decisions": [
                {"topic": "sql", "decision": "kept aggregate in SQL", "rationale": "set-based"}
            ],
            "warnings": [],
        }
    )


@pytest.fixture
def load_procedure() -> Callable[[str], str]:
    def load(name: str) -> str:
        return (PROCEDURES_DIR / f"{name}.sql").read_text(encoding="utf-8")

    return load


@pytest.fixture
def store() -> InMemoryDatabase:
    return InMemoryDatabase()


type GraphFactory = Callable[..., ModernizationGraph]
type ServiceFactory = Callable[..., ModernizationService]


@pytest.fixture
def make_graph(store: InMemoryDatabase) -> GraphFactory:
    """Real LangGraph graph + real parser/analyzer; fake LLM and in-memory database."""

    def factory(
        llm: FakeLLM | None = None,
        validator: CodeValidator | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> ModernizationGraph:
        return build_modernization_graph(
            parser=PglastParser(),
            analyzer=SemanticAnalyzer(),
            generation_service=CodeGenerationService(
                llm or FakeLLM([llm_payload()]), GenerationPromptBuilder()
            ),
            validator=validator or CompositeCodeValidator([PythonASTValidator(), RuffValidator()]),
            transactions=store,
            modernizations=InMemoryModernizationRepository(store),
            retry=retry,
        )

    return factory


@pytest.fixture
def make_service(store: InMemoryDatabase, make_graph: GraphFactory) -> ServiceFactory:
    def factory(**graph_options: Any) -> ModernizationService:
        return ModernizationService(
            LangGraphModernizationPipeline(make_graph(**graph_options)),
            store,
            InMemoryModernizationRepository(store),
        )

    return factory
