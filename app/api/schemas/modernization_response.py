from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.enums import ModernizationStatus
from app.domain.models.modernization import Modernization, ModernizationReport


class ModernizationResponse(BaseModel):
    execution_id: UUID
    status: ModernizationStatus
    generated_code: str | None
    report: ModernizationReport
    """The domain report is already a serializable contract; reused instead of mirrored."""
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, modernization: Modernization) -> ModernizationResponse:
        return cls(
            execution_id=modernization.id,
            status=modernization.status,
            generated_code=modernization.generated_code,
            report=modernization.report,
            created_at=modernization.created_at,
            updated_at=modernization.updated_at,
        )


class HealthResponse(BaseModel):
    status: str = "ok"
