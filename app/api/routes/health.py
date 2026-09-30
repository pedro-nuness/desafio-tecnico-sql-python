from fastapi import APIRouter

from app.api.schemas.modernization_response import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> HealthResponse:
    return HealthResponse()
