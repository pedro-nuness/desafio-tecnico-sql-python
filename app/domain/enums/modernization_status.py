from enum import StrEnum


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
