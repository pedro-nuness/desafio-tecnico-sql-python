from typing import Protocol
from uuid import UUID

from app.core.database.transaction import SessionTransactionManager
from app.features.modernization.domain.modernization import Modernization
from app.features.modernization.persistence import mapper
from app.features.modernization.persistence.models import ModernizationHistoryModel
from app.shared.errors import NotFoundError


class ModernizationRepository(Protocol):
    """Port: persistence of Modernization aggregates (in-memory fake in the tests).

    Works inside the transaction opened by the caller (app.shared.persistence); flushes,
    never commits: the caller does.
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
    """Uses the session of the transaction in progress (current_session()). Injected once,
    like any other dependency."""

    def __init__(self, transactions: SessionTransactionManager) -> None:
        self._transactions = transactions

    async def save(self, modernization: Modernization) -> None:
        session = self._transactions.current_session()
        session.add(mapper.to_model(modernization))
        await session.flush()

    async def update(self, modernization: Modernization) -> None:
        session = self._transactions.current_session()
        model = await session.get(ModernizationHistoryModel, modernization.id)
        if model is None:
            raise not_found(modernization.id)
        mapper.apply_to_model(modernization, model)
        await session.flush()

    async def get(self, modernization_id: UUID) -> Modernization:
        model = await self._transactions.current_session().get(
            ModernizationHistoryModel, modernization_id
        )
        if model is None:
            raise not_found(modernization_id)
        return mapper.to_domain(model)
