from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request

from app.features.modernization.api.schemas.modernization_request import (
    ModernizationRequest,
)
from app.features.modernization.api.schemas.modernization_response import (
    ModernizationResponse,
)
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.domain.models.modernization import PipelineProgress

# DishkaRoute injects every FromDishka[...] parameter from the container (core/providers.py).
router = APIRouter(tags=["modernization"], route_class=DishkaRoute)


@router.post("/modernize")
async def modernize(
    request: ModernizationRequest,
    http_request: Request,
    service: FromDishka[ModernizationService],
) -> ModernizationResponse:
    """Runs synchronously; on failure the run is already recorded and the global handler
    answers with its execution_id."""
    progress = PipelineProgress()
    http_request.state.modernization_progress = progress
    modernization = await service.modernize(
        request.source_code,
        request.schema_context,
        progress=progress,
    )
    return ModernizationResponse.from_domain(modernization)


@router.get("/modernizations/{execution_id}")
async def get_modernization(
    execution_id: UUID, service: FromDishka[ModernizationService]
) -> ModernizationResponse:
    modernization = await service.get(execution_id)
    return ModernizationResponse.from_domain(modernization)
