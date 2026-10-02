"""Case generation: the LLM proposes inputs, the original filters them, the caller's cases stay
the reference; in the graph it runs next to code generation and never fails the run."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.case_generation.generate_cases import GenerateCases
from app.features.modernization.code_generation.generate_code import GenerateCode
from app.features.modernization.code_generation.prompt import CodeGenerationPromptBuilder
from app.features.modernization.domain import ModernizationStatus, PipelineStep
from app.features.modernization.graph.builder import (
    ModernizationGraph,
    RetryPolicy,
    build_modernization_graph,
)
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.schemas import ModernizationRequest
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseSource,
    Scenario,
)
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.domain import ValidationMessage
from app.features.modernization.validation.validate_code import (
    Findings,
    Routine,
    Rule,
    ValidateCode,
)
from app.shared.integrations.errors import IntegrationError
from tests.conftest import llm_payload
from tests.fakes import FakeLLM, InMemoryDatabase, InMemoryModernizationRepository

EXAMPLES = Path(__file__).parents[2] / "examples"
SOURCE = (EXAMPLES / "procedures" / "b_fn_saldo_cliente.sql").read_text(encoding="utf-8")
SCHEMA = "CREATE TABLE contas (id serial, cliente_id bigint, saldo numeric, status text);"
USER = Scenario(
    setup_sql=f"{SCHEMA}\nINSERT INTO contas VALUES (1, 1, 100, 'ATIVA');",
    cases=(Case(name="client 1", sql="SELECT fn_saldo_cliente(1)", args=(1,)),),
    ignore_columns=("id",),
)


def _cases_answer(*cases: dict[str, Any], seed: str | None = None) -> str:
    return json.dumps({"seed": seed, "ignore_columns": ["id"], "cases": list(cases)})


def _case(name: str, client: Any = 2) -> dict[str, Any]:
    return {"name": name, "sql": f"SELECT fn_saldo_cliente({client})", "args": [client]}


class _Equivalence:
    """The harness port case generation uses: `probe` answers scripted reasons."""

    def __init__(self, reasons: dict[str, str] | None = None, *, configured: bool = True) -> None:
        self.configured = configured
        self._reasons = reasons or {}
        self.probed: list[dict[str, Any]] = []

    async def probe(
        self, *, source_code: str, setup_sql: str, cases: Sequence[Case]
    ) -> tuple[str | None, ...]:
        self.probed.append({"setup_sql": setup_sql, "cases": tuple(cases)})
        return tuple(self._reasons.get(case.name) for case in cases)


def _generate(llm: FakeLLM, equivalence: _Equivalence | None = None) -> GenerateCases:
    return GenerateCases(llm, equivalence or _Equivalence())  # type: ignore[arg-type]


async def _execute(generate: GenerateCases, behavior: Scenario | None = USER) -> Any:
    return await generate.execute(
        procedure=PglastParser().parse(SOURCE),
        source_code=SOURCE,
        schema_context=SCHEMA,
        behavior=behavior,
    )


# --------------------------------------------------------------------------- the step


async def test_kept_cases_join_the_callers_on_the_callers_setup() -> None:
    llm = FakeLLM(
        [
            _cases_answer(
                _case("inactive account"),
                _case("client 1"),  # same name as the caller's: renamed, not dropped
                {"name": "two args", "sql": "SELECT fn_saldo_cliente(1, 2)", "args": [1, 2]},
                {"name": "other routine", "sql": "SELECT now()", "args": [1]},
                {"sql": "SELECT fn_saldo_cliente(3)"},  # no name: off contract
                _case("bad column", 9),
                seed="INSERT INTO contas VALUES (9, 9, 1, 'ATIVA');",
            )
        ]
    )
    equivalence = _Equivalence({"bad column": "not valid SQL for this schema: ..."})

    scenario, result = await _execute(_generate(llm, equivalence))

    assert [case.name for case in scenario.cases] == ["client 1", "inactive account", "client 1 #2"]
    assert [case.source for case in scenario.cases] == [
        CaseSource.USER,
        CaseSource.GENERATED,
        CaseSource.GENERATED,
    ]
    assert scenario.setup_sql == USER.setup_sql  # the proposed seed is ignored
    assert scenario.ignore_columns == USER.ignore_columns
    assert equivalence.probed[0]["setup_sql"] == USER.setup_sql
    assert result.kept == ("inactive account", "client 1 #2")
    assert result.discarded == (
        "two args: 2 args, the routine takes 1",
        "other routine: does not call fn_saldo_cliente",
        "case #5: not in the contract (name, sql, args)",
        "bad column: not valid SQL for this schema: ...",
    )
    assert not result.seed_generated
    assert result.warnings == ("Ignored the proposed seed: the caller's setup is kept.",)
    # The prompt shows the caller's setup and cases so the LLM builds on them.
    [request] = llm.requests
    assert "INSERT INTO contas VALUES (1, 1, 100, 'ATIVA')" in request.user_prompt
    assert "- client 1: SELECT fn_saldo_cliente(1)" in request.user_prompt


async def test_without_the_callers_cases_the_llm_writes_the_seed_too() -> None:
    seed = "INSERT INTO contas VALUES (1, 1, 100, 'ATIVA');"
    llm = FakeLLM([_cases_answer(_case("client 1", 1), seed=seed)])

    scenario, result = await _execute(_generate(llm), behavior=None)

    assert scenario.setup_sql == f"{SCHEMA}\n{seed}"
    assert scenario.compare_tables == ()  # every table the setup creates
    assert scenario.ignore_columns == ("id",)
    assert result.seed_generated and result.kept == ("client 1",)


async def test_nothing_usable_and_no_callers_cases_means_no_scenario() -> None:
    llm = FakeLLM([_cases_answer(_case("broken"))])
    equivalence = _Equivalence({"broken": "the setup failed: ..."})

    scenario, result = await _execute(_generate(llm, equivalence), behavior=None)

    assert scenario is None
    assert result.discarded == ("broken: the setup failed: ...",)


async def test_an_answer_off_the_contract_is_an_integration_error() -> None:
    with pytest.raises(IntegrationError, match="case generation answer"):
        await _execute(_generate(FakeLLM(["no json here"])))


# --------------------------------------------------------------------------- in the graph


class _RecordingCheck:
    """Records the scenario validation received; fails the first time if asked to."""

    name = "recording"

    def __init__(self, *, fail_first: bool = False) -> None:
        self.behaviors: list[Scenario | None] = []
        self._fail_first = fail_first

    async def check(self, code: str, routine: Routine | None = None) -> Findings:
        self.behaviors.append(routine.behavior if routine else None)
        if self._fail_first and len(self.behaviors) == 1:
            return (ValidationMessage(code="X", message="fix it"),)
        return ()


def _graph(
    code_llm: FakeLLM, cases_llm: FakeLLM, check: _RecordingCheck, *, configured: bool = True
) -> ModernizationGraph:
    return build_modernization_graph(
        parser=PglastParser(),
        analyzer=SemanticAnalyzer(),
        generate_code=GenerateCode(code_llm, CodeGenerationPromptBuilder()),
        validate_code=ValidateCode(
            [Rule(PythonASTCheck(), blocking=True), Rule(check, blocking=False)]
        ),
        execution_log=ExecutionLog(InMemoryModernizationRepository(InMemoryDatabase())),
        generate_cases=_generate(cases_llm, _Equivalence(configured=configured)),
        retry=RetryPolicy(max_attempts=2),
    )


async def test_cases_are_generated_next_to_the_code_and_reach_validation() -> None:
    check = _RecordingCheck()
    cases_llm = FakeLLM([_cases_answer(_case("inactive account"))])

    final = await _graph(FakeLLM([llm_payload()]), cases_llm, check).ainvoke(
        {"source_code": SOURCE, "schema_context": SCHEMA, "behavior": USER}
    )

    modernization = final["modernization"]
    assert modernization.status is ModernizationStatus.SUCCESS
    steps = modernization.report.completed_steps
    assert {PipelineStep.CODE_GENERATION, PipelineStep.CASE_GENERATION} <= set(steps)
    assert steps[-1] is PipelineStep.VALIDATION  # validation ran once, after both
    assert steps.count(PipelineStep.VALIDATION) == 1
    assert modernization.report.case_generation.kept == ("inactive account",)
    [behavior] = check.behaviors
    assert behavior is not None
    assert [case.name for case in behavior.cases] == ["client 1", "inactive account"]


async def test_a_retry_regenerates_the_code_but_not_the_cases() -> None:
    check = _RecordingCheck(fail_first=True)
    code_llm = FakeLLM([llm_payload(), llm_payload()])
    cases_llm = FakeLLM([_cases_answer(_case("inactive account"))])

    await _graph(code_llm, cases_llm, check).ainvoke(
        {"source_code": SOURCE, "schema_context": SCHEMA, "behavior": USER}
    )

    assert len(code_llm.requests) == 2
    assert len(cases_llm.requests) == 1
    assert check.behaviors[0] == check.behaviors[1]  # same scenario on the retry


@pytest.mark.parametrize(
    ("graph_input", "configured"),
    [
        ({"generate_cases": False}, True),  # the caller's cases only
        ({}, False),  # no evaluation database: nothing could filter the cases
    ],
)
async def test_the_step_does_not_run_when_disabled_or_unavailable(
    graph_input: dict[str, Any], configured: bool
) -> None:
    check = _RecordingCheck()
    cases_llm = FakeLLM([_cases_answer(_case("inactive account"))])
    graph = _graph(FakeLLM([llm_payload()]), cases_llm, check, configured=configured)

    final = await graph.ainvoke(
        {"source_code": SOURCE, "schema_context": SCHEMA, "behavior": USER} | graph_input
    )

    report = final["modernization"].report
    assert PipelineStep.CASE_GENERATION not in report.completed_steps
    assert report.case_generation is None and cases_llm.requests == []
    assert check.behaviors == [USER]


@pytest.mark.parametrize(
    ("cases_llm", "graph_input", "warning"),
    [
        (
            FakeLLM(error=IntegrationError("provider down")),
            {"schema_context": SCHEMA},
            "Case generation failed, only the caller's cases run: provider down",
        ),
        (FakeLLM(), {}, "Case generation skipped: it needs `schema`"),
    ],
)
async def test_case_generation_never_fails_the_run(
    cases_llm: FakeLLM, graph_input: dict[str, Any], warning: str
) -> None:
    check = _RecordingCheck()

    final = await _graph(FakeLLM([llm_payload()]), cases_llm, check).ainvoke(
        {"source_code": SOURCE, "behavior": USER} | graph_input
    )

    modernization = final["modernization"]
    assert modernization.status is ModernizationStatus.SUCCESS
    assert PipelineStep.CASE_GENERATION in modernization.report.completed_steps
    assert any(w.startswith(warning) for w in modernization.report.warnings)
    assert check.behaviors == [USER]


# --------------------------------------------------------------------------- request


def test_the_request_generates_cases_unless_told_not_to() -> None:
    assert ModernizationRequest(source_code="src").to_command().generate_cases
    request = ModernizationRequest.model_validate({"source_code": "src", "generate_cases": False})
    assert request.to_command().generate_cases is False


def test_the_callers_cases_are_always_marked_as_theirs() -> None:
    request = ModernizationRequest.model_validate(
        {
            "source_code": "src",
            "schema": SCHEMA,
            "behavior": {
                "seed": "",
                "cases": [{"name": "c", "sql": "SELECT f()", "source": "generated"}],
            },
        }
    )

    behavior = request.to_command().behavior
    assert behavior is not None and behavior.cases[0].source is CaseSource.USER
