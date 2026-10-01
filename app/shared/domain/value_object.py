from pydantic import BaseModel, ConfigDict


class ValueObject(BaseModel):
    """Immutable, JSON-serializable domain value (shared Pydantic configuration only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
