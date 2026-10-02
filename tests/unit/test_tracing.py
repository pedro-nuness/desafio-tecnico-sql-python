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
    )
    generation = AsyncMock()
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
    error = RuntimeError("provider failed")
    with pytest.raises(RuntimeError, match="provider failed"):
        await TracedLLM(FakeLLM(error=error), model="primary").generate(request)
    generation.on_llm_error.assert_awaited_once_with(error)
