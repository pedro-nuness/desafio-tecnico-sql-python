"""HTTP contract: requests become commands, domain results become responses."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.domain.modernization import Modernization, ModernizationReport
from app.features.modernization.use_cases import ModernizeCommand


class ModernizationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_code: str = Field(min_length=1, description="CREATE FUNCTION/PROCEDURE ... PL/pgSQL")
    schema_context: str | None = Field(
        default=None,
        alias="schema",
        description="Optional DDL of the tables involved (improves generation).",
    )

    def to_command(self) -> ModernizeCommand:
        return ModernizeCommand(source_code=self.source_code, schema_context=self.schema_context)


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
