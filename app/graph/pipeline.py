import logging
from typing import cast
from uuid import UUID

from app.domain.enums import PipelineStep
from app.domain.models.modernization import PipelineError, PipelineOutcome
from app.graph.builder import ModernizationGraph
from app.graph.state import ModernizationState, initial_state

logger = logging.getLogger(__name__)


class LangGraphModernizationPipeline:
    """ModernizationPipeline port implemented with a compiled LangGraph graph.

    Streams full-state snapshots so that, if anything unexpected escapes a node, the
    last snapshot still tells which steps completed.
    """

    def __init__(self, graph: ModernizationGraph) -> None:
        self._graph = graph

    async def run(
        self,
        *,
        execution_id: UUID,
        source_code: str,
        schema_context: str | None,
    ) -> PipelineOutcome:
        state = initial_state(
            execution_id=execution_id, source_code=source_code, schema_context=schema_context
        )
        last_snapshot = state
        try:
            async for snapshot in self._graph.astream(state, stream_mode="values"):
                last_snapshot = cast(ModernizationState, snapshot)
        except Exception as exc:  # boundary: the port contract says run() never raises
            logger.exception("pipeline crashed", extra={"execution_id": str(execution_id)})
            crash = PipelineError(
                step=_next_step(last_snapshot["completed_steps"]),
                error_type=type(exc).__name__,
                message=str(exc),
            )
            return to_outcome(last_snapshot, extra_errors=(crash,))
        return to_outcome(last_snapshot)


def _next_step(completed: list[PipelineStep]) -> PipelineStep | None:
    return next((step for step in PipelineStep if step not in completed), None)


def to_outcome(
    state: ModernizationState, extra_errors: tuple[PipelineError, ...] = ()
) -> PipelineOutcome:
    return PipelineOutcome(
        parsed_procedure=state.get("parsed_procedure"),
        semantic_analysis=state.get("semantic_analysis"),
        generation=state.get("generation"),
        validation=state.get("validation_result"),
        completed_steps=tuple(state.get("completed_steps", [])),
        errors=tuple(state.get("errors", [])) + extra_errors,
        warnings=tuple(state.get("warnings", [])),
    )
