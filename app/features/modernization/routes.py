from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request

from app.features.modernization.domain.modernization import PipelineProgress
from app.features.modernization.schemas import (
    EvaluationResponse,
    EvaluationSummaryResponse,
    ModernizationRequest,
    ModernizationResponse,
)
from app.features.modernization.use_cases import (
    EvaluateCommand,
    EvaluateModernization,
    EvaluationSummaryQuery,
    GetEvaluationSummary,
    GetModernization,
    GetModernizationQuery,
    ModernizeRoutine,
)

# DishkaRoute injects every FromDishka[...] parameter from the container (core/providers.py).
# Every route: request -> command -> use case -> response.
router = APIRouter(tags=["modernization"], route_class=DishkaRoute)


@router.post("/modernize")
async def modernize(
    request: ModernizationRequest,
    http_request: Request,
    use_case: FromDishka[ModernizeRoutine],
) -> ModernizationResponse:
    """Runs synchronously; on failure the run is already recorded and the global handler
    answers with its execution_id."""
    progress = PipelineProgress()
    http_request.state.modernization_progress = progress
    result = await use_case.execute(request.to_command(), progress=progress)
    return ModernizationResponse.from_domain(result)


@router.get("/modernizations/{execution_id}")
async def get_modernization(
    execution_id: UUID, use_case: FromDishka[GetModernization]
) -> ModernizationResponse:
    result = await use_case.execute(GetModernizationQuery(execution_id))
    return ModernizationResponse.from_domain(result)


@router.post("/modernizations/{execution_id}/evaluation")
async def evaluate_modernization(
    execution_id: UUID, use_case: FromDishka[EvaluateModernization]
) -> EvaluationResponse:
    """Behavioral equivalence of a recorded execution against the evaluation dataset
    (examples/evaluation); the result is stored in evaluation_results."""
    result = await use_case.execute(EvaluateCommand(execution_id))
    return EvaluationResponse.from_domain(result)


@router.get("/evaluations")
async def evaluation_summary(
    use_case: FromDishka[GetEvaluationSummary],
) -> EvaluationSummaryResponse:
    """The metric over the latest evaluation of each routine (e.g. annexes B-F)."""
    result = await use_case.execute(EvaluationSummaryQuery())
    return EvaluationSummaryResponse.from_domain(result)
