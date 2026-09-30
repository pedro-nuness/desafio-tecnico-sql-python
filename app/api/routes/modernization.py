from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import ModernizationServiceDep
from app.api.schemas.modernization_request import ModernizationRequest
from app.api.schemas.modernization_response import ModernizationResponse
from app.domain.exceptions import ModernizationNotFoundError

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
    try:
        modernization = await service.get(execution_id)
    except ModernizationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ModernizationResponse.from_domain(modernization)
