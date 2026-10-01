from app.features.modernization.domain.models.modernization import Modernization
from app.features.modernization.graph.builder import ModernizationGraph
from app.features.modernization.graph.state import ModernizationInput


class LangGraphModernizationPipeline:
    """ModernizationPipeline port implemented with a compiled LangGraph graph.

    The graph itself records the run (record_start / record_result) and turns node
    failures into report errors, so this adapter only invokes it and returns the
    persisted aggregate.
    """

    def __init__(self, graph: ModernizationGraph) -> None:
        self._graph = graph

    async def run(self, *, source_code: str, schema_context: str | None) -> Modernization:
        final = await self._graph.ainvoke(
            ModernizationInput(source_code=source_code, schema_context=schema_context)
        )
        modernization: Modernization = final["modernization"]
        return modernization
