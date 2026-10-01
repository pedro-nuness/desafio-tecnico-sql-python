from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from app.features.modernization.application.ports.repositories.modernization_repository import (
    ModernizationRepository,
)


class UnitOfWork(Protocol):
    """One transaction spanning every repository.

    New aggregates (e.g. `evaluations`, `llm_calls`) are added as new attributes here
    and in the adapter; existing repositories are untouched. Leaving the context
    without commit() rolls back.
    """

    modernizations: ModernizationRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


type UnitOfWorkFactory = Callable[[], UnitOfWork]
"""Each use-case transaction asks for a fresh unit of work."""
