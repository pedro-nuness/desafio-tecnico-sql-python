from enum import StrEnum


class GenerationStrategy(StrEnum):
    """Where the relational logic lives after modernization."""

    DATABASE_DELEGATED = "database_delegated"
    """Set-based SQL stays in PostgreSQL; Python only orchestrates the call."""

    PYTHON_REIMPLEMENTATION = "python_reimplementation"
    """Logic is rewritten in Python (no relational work to keep in the database)."""

    HYBRID = "hybrid"
    """Python owns control flow and errors; relational operations remain parameterized SQL."""


class ModernizationStatus(StrEnum):
    """Lifecycle of a modernization execution.

    RUNNING  -> persisted before the pipeline starts (a crash leaves a visible trace).
    SUCCESS  -> code generated and every validator passed.
    PARTIAL  -> code generated and syntactically valid, but non-blocking validators
                reported issues or a late step failed.
    FAILURE  -> no usable code (an early step failed or the code is not valid Python).
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


class PipelineStep(StrEnum):
    PARSING = "parsing"
    SEMANTIC_ANALYSIS = "semantic_analysis"
    GENERATION = "generation"
    VALIDATION = "validation"
