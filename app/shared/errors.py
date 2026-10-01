"""Application errors: one generic error per layer role, carrying a message and a payload.

There is no class per failure case. The class says *who* is at fault (the HTTP status is
derived from it in core/exception_handlers.py); the message says what happened; the
payload carries structured, public data for the client and the execution report.

Messages and payload are public: never put SDK texts, secrets or stack details in them
(keep those in `__cause__`, which is logged).
"""

from pydantic import JsonValue


class AppError(Exception):
    """Our own failure (bug, broken invariant, tool that could not run). HTTP 500."""

    def __init__(self, message: str, /, **payload: JsonValue) -> None:
        super().__init__(message)
        self.message = message
        self.payload = payload


class DomainError(AppError):
    """The input violates a domain rule (e.g. unsupported or invalid PL/pgSQL). HTTP 400."""


class NotFoundError(AppError):
    """The requested resource does not exist. HTTP 404."""
