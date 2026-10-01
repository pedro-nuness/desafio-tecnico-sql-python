from uuid import UUID

from fastapi import APIRouter, Request

from app.features.modernization.api.dependencies import ModernizationServiceDep
from app.features.modernization.api.schemas.modernization_request import (
    ModernizationRequest,
)
from app.features.modernization.api.schemas.modernization_response import (
    ModernizationResponse,
)
from app.features.modernization.domain.models.modernization import PipelineProgress

router = APIRouter(tags=["modernization"])


@router.post("/modernize")
async def modernize(
    request: ModernizationRequest, http_request: Request, service: ModernizationServiceDep
) -> ModernizationResponse:
    """Runs synchronously; exceptions reach the global handler with a progress snapshot."""
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
    execution_id: UUID, service: ModernizationServiceDep
) -> ModernizationResponse:
    modernization = await service.get(execution_id)
    return ModernizationResponse.from_domain(modernization)
