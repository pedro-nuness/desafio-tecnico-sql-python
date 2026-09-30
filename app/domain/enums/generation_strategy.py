from enum import StrEnum


class GenerationStrategy(StrEnum):
    """Where the relational logic lives after modernization."""

    DATABASE_DELEGATED = "database_delegated"
    """Set-based SQL stays in PostgreSQL; Python only orchestrates the call."""

    PYTHON_REIMPLEMENTATION = "python_reimplementation"
    """Logic is rewritten in Python (no relational work to keep in the database)."""

    HYBRID = "hybrid"
    """Python owns control flow and errors; relational operations remain parameterized SQL."""
