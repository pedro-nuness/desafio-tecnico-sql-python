from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.features.modernization.domain.modernization import Modernization
from app.features.modernization.persistence.models import ModernizationHistoryModel
from app.shared.errors import NotFoundError


class ModernizationRepository(Protocol):
    """Port: persistence of Modernization aggregates (in-memory fake in the tests).

    Every write is its own short transaction, committed before returning.
    """

    async def save(self, modernization: Modernization) -> None: ...

    async def update(self, modernization: Modernization) -> None:
        """Raises NotFoundError when the aggregate was never saved."""
        ...

    async def get(self, modernization_id: UUID) -> Modernization:
        """Raises NotFoundError when there is no such aggregate."""
        ...


def not_found(modernization_id: UUID) -> NotFoundError:
    return NotFoundError(
        f"Modernization {modernization_id} not found", execution_id=str(modernization_id)
    )


class SqlAlchemyModernizationRepository:
    """One session per operation: concurrent runs never share a session."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, modernization: Modernization) -> None:
        async with self._sessions() as session:
            session.add(ModernizationHistoryModel.from_domain(modernization))
            await session.commit()

    async def update(self, modernization: Modernization) -> None:
        async with self._sessions() as session:
            model = await session.get(ModernizationHistoryModel, modernization.id)
            if model is None:
                raise not_found(modernization.id)
            model.update_from(modernization)
            await session.commit()

    async def get(self, modernization_id: UUID) -> Modernization:
        async with self._sessions() as session:
            model = await session.get(ModernizationHistoryModel, modernization_id)
            if model is None:
                raise not_found(modernization_id)
            return model.to_domain()
