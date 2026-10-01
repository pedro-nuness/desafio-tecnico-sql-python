from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.modernization.domain.exceptions import ModernizationNotFoundError
from app.features.modernization.domain.models.modernization import Modernization
from app.features.modernization.infrastructure.persistence.mappers import (
    modernization_mapper as mapper,
)
from app.features.modernization.infrastructure.persistence.models import (
    ModernizationHistoryModel,
)


class SqlAlchemyModernizationRepository:
    """Works inside the session owned by the unit of work; flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, modernization: Modernization) -> None:
        self._session.add(mapper.to_model(modernization))
        await self._session.flush()

    async def update(self, modernization: Modernization) -> None:
        model = await self._session.get(ModernizationHistoryModel, modernization.id)
        if model is None:
            raise ModernizationNotFoundError(modernization.id)
        mapper.apply_to_model(modernization, model)
        await self._session.flush()

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None:
        model = await self._session.get(ModernizationHistoryModel, modernization_id)
        return mapper.to_domain(model) if model else None
