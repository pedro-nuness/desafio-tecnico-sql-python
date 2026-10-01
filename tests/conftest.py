import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.generation.generate_code import GenerateCode
from app.features.modernization.generation.prompt import GenerationPromptBuilder
from app.features.modernization.graph.builder import (
    DEFAULT_RETRY,
    ModernizationGraph,
    RetryPolicy,
    build_modernization_graph,
)
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.use_cases import GetModernization, ModernizeRoutine
from app.features.modernization.validation.checks.lint import RuffCheck
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from tests.fakes import FakeLLM, InMemoryDatabase, InMemoryModernizationRepository

ENV_TEMPLATE = Path(__file__).parents[1] / ".env.example"


def _settings_from_the_template() -> None:
    """Settings has no defaults and the app reads only .env: tests use the template's values
    (no secrets) instead of whatever .env the developer has. Runs at import, before any test
    module imports the app (app.core.server builds Settings at import). Variables already in
    the environment win (e.g. EVALUATION_DATABASE_URL for the integration tests);
    TEST_DATABASE_URL stays opt-in, so integration tests still skip without a database."""
    for name, value in dotenv_values(ENV_TEMPLATE).items():
        if value is not None and name != "TEST_DATABASE_URL":
            os.environ.setdefault(name, value)


_settings_from_the_template()


@pytest.fixture(autouse=True)
def disable_external_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")


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
type ModernizeFactory = Callable[..., ModernizeRoutine]


def default_validate_code() -> ValidateCode:
    """Same policy as core/providers.py."""
    return ValidateCode([Rule(PythonASTCheck(), blocking=True), Rule(RuffCheck(), blocking=False)])


@pytest.fixture
def make_graph(store: InMemoryDatabase) -> GraphFactory:
    """Real LangGraph graph + real parser/analyzer; fake LLM and in-memory database."""

    def factory(
        llm: FakeLLM | None = None,
        validate_code: ValidateCode | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> ModernizationGraph:
        return build_modernization_graph(
            parser=PglastParser(),
            analyzer=SemanticAnalyzer(),
            generate_code=GenerateCode(llm or FakeLLM([llm_payload()]), GenerationPromptBuilder()),
            validate_code=validate_code or default_validate_code(),
            execution_log=ExecutionLog(InMemoryModernizationRepository(store)),
            retry=retry,
        )

    return factory


@pytest.fixture
def make_modernize(make_graph: GraphFactory) -> ModernizeFactory:
    def factory(**graph_options: Any) -> ModernizeRoutine:
        return ModernizeRoutine(make_graph(**graph_options))

    return factory


@pytest.fixture
def get_modernization(store: InMemoryDatabase) -> GetModernization:
    return GetModernization(InMemoryModernizationRepository(store))
