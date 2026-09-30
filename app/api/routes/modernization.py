from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import ModernizationServiceDep
from app.api.schemas.modernization_request import ModernizationRequest
from app.api.schemas.modernization_response import ModernizationResponse

router = APIRouter(tags=["modernization"])


@router.post("/modernize")
async def modernize(
    request: ModernizationRequest, service: ModernizationServiceDep
) -> ModernizationResponse:
    """Runs the pipeline synchronously. Pipeline failures are part of the answer
    (status=failure/partial + report), not HTTP errors: the execution was recorded."""
    modernization = await service.modernize(request.source_code, request.schema_context)
    return ModernizationResponse.from_domain(modernization)


@router.get("/modernizations/{execution_id}")
async def get_modernization(
    execution_id: UUID, service: ModernizationServiceDep
) -> ModernizationResponse:
    modernization = await service.get(execution_id)
    return ModernizationResponse.from_domain(modernization)
