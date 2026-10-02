from unittest.mock import AsyncMock, MagicMock

import pytest

from app.shared.integrations.llm.llm import LLMRequest, LLMResponse
from app.shared.integrations.llm.tracing import TracedLLM
from tests.fakes import FakeLLM


async def test_generation_is_a_child_of_its_graph_node(make_graph, load_procedure):
    from langchain_core.callbacks import AsyncCallbackHandler

    from app.features.modernization.graph.builder import run_modernization

    class Recorder(AsyncCallbackHandler):
        def __init__(self):
            self.parent = None
            self.nodes = {}

        async def on_chain_start(self, serialized, inputs, *, run_id, name=None, **kwargs):
            self.nodes[run_id] = name

        async def on_llm_start(self, serialized, prompts, *, parent_run_id=None, **kwargs):
            self.parent = parent_run_id

    callback = Recorder()
    graph = make_graph(llm=TracedLLM(FakeLLM(), model="primary")).with_config(callbacks=[callback])
    await run_modernization(
        graph,
        source_code=load_procedure("process_orders"),
        schema_context=None,
    )
    assert callback.parent is not None
    assert callback.nodes[callback.parent] == "code_generation"


async def test_container_enables_tracing_only_with_both_keys(monkeypatch):
    from app.core.bootstrap import build_container
    from app.core.config.settings import Settings
    from app.features.modernization.graph.builder import ModernizationGraph
    from app.features.modernization.validation.checks.behavior.harness import (
        BehavioralEquivalence,
    )
    from app.features.modernization.validation.checks.behavior.tracing import TracedEquivalence
    from app.shared.integrations.llm.llm import LLM

    factory = MagicMock()
    monkeypatch.setattr("app.core.providers.Langfuse", factory)
    callback = MagicMock()
    monkeypatch.setattr("app.core.providers.CallbackHandler", callback)
    for enabled in (False, True):
        settings = Settings(
            _env_file=None,
            llm_api_key="fake",
            langfuse_public_key="pk-test" if enabled else None,
            langfuse_secret_key="sk-test" if enabled else None,
        )
        container = build_container(settings)
        try:
            assert isinstance(await container.get(LLM), TracedLLM) is enabled
            harness = await container.get(BehavioralEquivalence)
            assert isinstance(harness, TracedEquivalence) is enabled
            graph = await container.get(ModernizationGraph)
            if enabled:
                assert graph.config["callbacks"] == [callback.return_value]
                callback.assert_called_once_with(public_key="pk-test")
            else:
                factory.assert_not_called()
        finally:
            await container.close()
    factory.return_value.shutdown.assert_called_once()


async def test_tracing_preserves_response_usage_and_errors(monkeypatch):
    response = LLMResponse(
        content="generated",
        provider="fake",
        model="test",
        latency_ms=1,
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.0021,
    )
    generation = AsyncMock()
    observation = MagicMock()
    monkeypatch.setattr(
        "app.shared.integrations.llm.tracing.langfuse_observation", lambda run: observation
    )
    manager = AsyncMock(on_llm_start=AsyncMock(return_value=[generation]))
    monkeypatch.setattr(
        "app.shared.integrations.llm.tracing.get_async_callback_manager_for_config",
        lambda config: manager,
    )
    llm = TracedLLM(AsyncMock(generate=AsyncMock(return_value=response)), model="primary")
    request = LLMRequest(system_prompt="rules", user_prompt="source")
    assert await llm.generate(request) == response
    # Langfuse reads the model at start (else it warns); the end reports who answered
    assert manager.on_llm_start.call_args.kwargs["invocation_params"]["model"] == "primary"
    assert generation.on_llm_end.call_args.args[0].llm_output["model_name"] == "test"
    assert generation.on_llm_end.call_args.args[0].llm_output["token_usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
    }
    # The callback drops cost: it goes straight to the Langfuse generation
    observation.update.assert_called_once_with(cost_details={"total": 0.0021})
    error = RuntimeError("provider failed")
    with pytest.raises(RuntimeError, match="provider failed"):
        await TracedLLM(FakeLLM(error=error), model="primary").generate(request)
    generation.on_llm_error.assert_awaited_once_with(error)


async def test_behavior_run_traces_each_case_under_the_node_and_scores_the_pass_rate(monkeypatch):
    from langchain_core.callbacks import AsyncCallbackHandler
    from langchain_core.runnables import RunnableLambda

    from app.features.modernization.validation.checks.behavior.domain import (
        Case,
        CaseResult,
        Scenario,
    )
    from app.features.modernization.validation.checks.behavior.harness import (
        BehavioralEquivalence,
    )
    from app.features.modernization.validation.checks.behavior.tracing import TracedEquivalence

    class Recorder(AsyncCallbackHandler):
        """Stands for Langfuse's handler: records the spans, keeps an observation per run."""

        def __init__(self):
            self.spans = {}
            self.errors = {}
            self._runs = {}

        async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
            self.spans[run_id] = (kw.get("name"), parent_run_id)
            self._runs[run_id] = MagicMock()

        async def on_chain_error(self, error, *, run_id, **kwargs):
            self.errors[self.spans[run_id][0]] = str(error)

    results = (
        CaseResult(name="ok", passed=True, detail="same", original="o", generated="g"),
        CaseResult(name="bad", passed=False, detail="differs", original="1", generated="2"),
    )

    async def run(self, **request):
        return results

    monkeypatch.setattr(BehavioralEquivalence, "run", run)
    monkeypatch.setattr("app.shared.integrations.tracing.CallbackHandler", Recorder)
    scenario = Scenario(
        setup_sql="",
        cases=tuple(Case(name=r.name, sql="SELECT 1") for r in results),
    )
    harness = TracedEquivalence(None)

    async def validation(_):
        return await harness.run(
            routine="r", source_code="", code="", parameters=(), scenario=scenario
        )

    callback = Recorder()
    assert (
        await RunnableLambda(validation, name="validation").ainvoke(
            None, config={"callbacks": [callback]}
        )
        == results
    )

    ids = {name: run_id for run_id, (name, _) in callback.spans.items()}
    parents = {
        name: callback.spans[parent][0] for name, parent in callback.spans.values() if parent
    }
    assert parents == {
        "behavior.run": "validation",
        "case: ok": "behavior.run",
        "case: bad": "behavior.run",
    }
    assert callback.errors == {"case: bad": "differs. The original 1; the generated code 2"}
    callback._runs[ids["behavior.run"]].score.assert_called_once_with(
        name="behavior_pass_rate", value=0.5, comment="1/2 cases"
    )
