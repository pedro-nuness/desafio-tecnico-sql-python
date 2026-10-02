"""Native LLM callbacks inherit the current graph node's trace context."""

from langchain_core.outputs import Generation, LLMResult
from langchain_core.runnables.config import ensure_config, get_async_callback_manager_for_config

from app.shared.integrations.llm.llm import LLM, LLMRequest, LLMResponse
from app.shared.integrations.tracing import langfuse_observation


class TracedLLM:
    def __init__(self, llm: LLM, *, model: str) -> None:
        self._llm = llm
        self._model = model
        """Primary route's model: Langfuse needs one at start; the end reports the one that
        answered (another route after a failover)."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        manager = get_async_callback_manager_for_config(ensure_config())
        (generation,) = await manager.on_llm_start(
            {"name": "llm.generate"},
            [request.system_prompt + "\n\n" + request.user_prompt],
            name="llm.generate",
            invocation_params={
                "model": self._model,
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            },
        )
        # Needed: close the native generation callback on provider failures too.
        try:
            response = await self._llm.generate(request)
        except Exception as exc:
            await generation.on_llm_error(exc)
            raise
        # The callback carries tokens, not cost: without it Langfuse only prices models it knows.
        observation = langfuse_observation(generation)
        if observation is not None and response.cost_usd is not None:
            observation.update(cost_details={"total": response.cost_usd})
        await generation.on_llm_end(
            LLMResult(
                generations=[[Generation(text=response.content)]],
                llm_output={
                    "model_name": response.model,
                    "token_usage": {
                        key: value
                        for key, value in {
                            "prompt_tokens": response.input_tokens,
                            "completion_tokens": response.output_tokens,
                        }.items()
                        if value is not None
                    },
                },
            )
        )
        return response
