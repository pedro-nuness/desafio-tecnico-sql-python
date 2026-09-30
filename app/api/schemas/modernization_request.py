from pydantic import BaseModel, ConfigDict, Field


class ModernizationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_code: str = Field(min_length=1, description="CREATE FUNCTION/PROCEDURE ... PL/pgSQL")
    schema_context: str | None = Field(
        default=None,
        alias="schema",
        description="Optional DDL of the tables involved (improves generation).",
    )
