"""In-memory test doubles for the persistence ports (no database needed)."""

from types import TracebackType
from typing import Self
from uuid import UUID

from app.domain.exceptions import ModernizationNotFoundError
from app.domain.models.modernization import Modernization


class InMemoryModernizationRepository:
    def __init__(self, committed: dict[UUID, Modernization]) -> None:
        self._committed = committed
        self.pending: dict[UUID, Modernization] = {}

    async def save(self, modernization: Modernization) -> None:
        self.pending[modernization.id] = modernization

    async def update(self, modernization: Modernization) -> None:
        if modernization.id not in self._committed and modernization.id not in self.pending:
            raise ModernizationNotFoundError(modernization.id)
        self.pending[modernization.id] = modernization

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None:
        return self.pending.get(modernization_id) or self._committed.get(modernization_id)


class InMemoryUnitOfWork:
    """Mimics transactional semantics: only commit() publishes pending writes."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.modernizations = InMemoryModernizationRepository(store.rows)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self._store.rows.update(self.modernizations.pending)
        self._store.history.extend(self.modernizations.pending.values())
        self.modernizations.pending.clear()

    async def rollback(self) -> None:
        self.modernizations.pending.clear()


class InMemoryStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, Modernization] = {}
        self.history: list[Modernization] = []
        """Every committed version, in order (lets tests see RUNNING -> final)."""

    def uow(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)
