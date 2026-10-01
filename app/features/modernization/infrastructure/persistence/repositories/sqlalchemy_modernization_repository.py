from uuid import UUID

from app.core.database.transaction import SessionTransactionManager
from app.features.modernization.domain.models.modernization import Modernization
from app.features.modernization.infrastructure.persistence.mappers import (
    modernization_mapper as mapper,
)
from app.features.modernization.infrastructure.persistence.models import (
    ModernizationHistoryModel,
)
from app.shared.errors import NotFoundError


class SqlAlchemyModernizationRepository:
    """Works inside the transaction opened by the caller (current session); flushes, never
    commits. Injected once, like any other dependency."""

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
            raise NotFoundError(
                f"Modernization {modernization.id} not found", execution_id=str(modernization.id)
            )
        mapper.apply_to_model(modernization, model)
        await session.flush()

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None:
        model = await self._transactions.current_session().get(
            ModernizationHistoryModel, modernization_id
        )
        return mapper.to_domain(model) if model else None
