from app.features.modernization.domain.models.modernization import Modernization, PipelineProgress
from app.features.modernization.graph.builder import ModernizationGraph
from app.features.modernization.graph.state import ModernizationInput


class LangGraphModernizationPipeline:
    """ModernizationPipeline port implemented with a compiled LangGraph graph.

    The graph records every run (normal result or the failing step) and exposes the
    execution id through `progress`. Exceptions propagate after being recorded.
    """

    def __init__(self, graph: ModernizationGraph) -> None:
        self._graph = graph

    async def run(
        self,
        *,
        source_code: str,
        schema_context: str | None,
        progress: PipelineProgress | None = None,
    ) -> Modernization:
        final = await self._graph.ainvoke(
            ModernizationInput(source_code=source_code, schema_context=schema_context),
            config={"configurable": {"progress": progress}},
        )
        modernization: Modernization = final["modernization"]
        return modernization
