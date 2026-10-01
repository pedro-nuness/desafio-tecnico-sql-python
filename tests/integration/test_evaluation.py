"""The behavioral-equivalence harness and the evaluation repository on a real PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.features.modernization.domain import (
    Modernization,
    ModernizationReport,
    ModernizationStatus,
    ParsingSummary,
)
from app.features.modernization.evaluation.domain import Evaluation
from app.features.modernization.evaluation.repository import SqlAlchemyEvaluationRepository
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.persistence.repository import SqlAlchemyModernizationRepository
from app.features.modernization.validation.checks.behavior.check import BehaviorCheck
from app.features.modernization.validation.checks.behavior.domain import CaseResult
from app.features.modernization.validation.checks.behavior.harness import BehavioralEquivalence
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from tests.conftest import GraphFactory, llm_payload
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

EXAMPLES = Path(__file__).parents[2] / "examples"

type SessionFactory = async_sessionmaker[AsyncSession]

FAITHFUL_B = """
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    result = await conn.execute(
        text("SELECT COALESCE(SUM(saldo), 0) FROM contas "
             "WHERE cliente_id = :id AND status = 'ATIVA'"),
        {"id": p_cliente_id},
    )
    return result.scalar_one()
"""

# Forgets the status filter: right for client 3, wrong for client 1 (inactive account).
UNFAITHFUL_B = FAITHFUL_B.replace(" AND status = 'ATIVA'", "")

# Validates like the original, but never writes the transfer.
LAZY_D = """
from decimal import Decimal


class TransferenciaError(Exception):
    pass


async def sp_transferir_entre_contas(conn, origem: int, destino: int, valor: Decimal) -> None:
    if valor is None or valor <= 0 or origem == destino:
        raise TransferenciaError("invalid")
"""


def _modernization(annex: str, code: str) -> Modernization:
    source = (EXAMPLES / "procedures" / f"{annex}.sql").read_text(encoding="utf-8")
    parsing = ParsingSummary.of(PglastParser().parse(source))
    return Modernization.start(source).model_copy(
        update={"generated_code": code, "report": ModernizationReport(parsing=parsing)}
    )


@pytest.fixture
async def harness(migrated_database_url: str) -> BehavioralEquivalence:
    metric = BehavioralEquivalence(
        os.environ["TEST_DATABASE_URL"], EXAMPLES / "evaluation" / "scenarios.yml"
    )
    yield metric  # type: ignore[misc]
    await metric.close()


async def test_a_faithful_translation_is_equivalent_in_every_case(
    harness: BehavioralEquivalence,
) -> None:
    cases = await harness.evaluate(_modernization("b_fn_saldo_cliente", FAITHFUL_B))

    assert [c.passed for c in cases] == [True, True, True], [c.detail for c in cases]


async def test_a_wrong_result_is_caught_with_both_values(harness: BehavioralEquivalence) -> None:
    cases = await harness.evaluate(_modernization("b_fn_saldo_cliente", UNFAITHFUL_B))

    first, without_active, _ = cases
    assert not first.passed
    assert "result differs" in first.detail and "1500" in first.detail and "1800" in first.detail
    assert without_active.passed is False  # client 3 has a closed account: 50.00 vs 0


async def test_missing_writes_fail_and_deliberate_errors_pass(
    harness: BehavioralEquivalence,
) -> None:
    cases = {
        c.name: c
        for c in await harness.evaluate(_modernization("d_sp_transferir_entre_contas", LAZY_D))
    }

    ok = cases["transfer between active accounts"]
    assert not ok.passed
    assert "contas: 2 row(s) only in the original" in ok.detail
    assert cases["same account"].passed  # both raised; the Python error is its own class
    assert not cases["insufficient balance"].passed  # original raised, Python returned


async def test_every_sandbox_schema_is_dropped(
    harness: BehavioralEquivalence, session_factory: SessionFactory
) -> None:
    await harness.evaluate(_modernization("b_fn_saldo_cliente", FAITHFUL_B))

    async with session_factory() as session:
        left = await session.scalar(
            text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'eval\\_%'")
        )
    assert left == 0


async def test_repository_keeps_every_evaluation_and_reads_the_latest_per_routine(
    session_factory: SessionFactory,
) -> None:
    modernizations = SqlAlchemyModernizationRepository(session_factory)
    evaluations = SqlAlchemyEvaluationRepository(session_factory)
    modernization = _modernization("b_fn_saldo_cliente", FAITHFUL_B)
    case = CaseResult(name="c", passed=True, detail="d", original="o", generated="g")
    older = Evaluation.of(modernization, (case,)).model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(hours=1)}
    )
    newer = Evaluation.of(modernization, (case, case.model_copy(update={"passed": False})))

    await modernizations.save(modernization)
    await evaluations.save(older)
    await evaluations.save(newer)
    latest = await evaluations.latest_per_procedure()

    assert latest == (newer,)
    assert (latest[0].cases_passed, latest[0].cases_total) == (1, 2)


async def test_the_repair_loop_fixes_a_divergence_without_seeing_the_holdout_cases(
    harness: BehavioralEquivalence, make_graph: GraphFactory
) -> None:
    """Graph + real harness (subprocess): a wrong B is regenerated with the behavior findings
    of the dev cases as feedback; holdout case names never reach the prompt."""
    llm = FakeLLM(
        [
            llm_payload(code=UNFAITHFUL_B, strategy="database_delegated"),
            llm_payload(code=FAITHFUL_B, strategy="database_delegated"),
        ]
    )
    validate_code = ValidateCode(
        [Rule(PythonASTCheck(), blocking=True), Rule(BehaviorCheck(harness), blocking=False)]
    )
    source = (EXAMPLES / "procedures" / "b_fn_saldo_cliente.sql").read_text(encoding="utf-8")

    final = await make_graph(llm=llm, validate_code=validate_code).ainvoke({"source_code": source})

    modernization = final["modernization"]
    assert modernization.status is ModernizationStatus.SUCCESS
    assert modernization.generated_code == FAITHFUL_B
    _, retry = llm.requests
    assert "[behavior]" in retry.user_prompt
    assert "active accounts only" in retry.user_prompt and "1800" in retry.user_prompt
    assert "client without active accounts" not in retry.user_prompt  # holdout
