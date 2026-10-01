from typing import Protocol
from uuid import UUID

from app.features.modernization.domain.models.modernization import Modernization


class ModernizationRepository(Protocol):
    """Persistence of Modernization aggregates. Never commits: the UnitOfWork does."""

    async def save(self, modernization: Modernization) -> None: ...

    async def update(self, modernization: Modernization) -> None:
        """Raises ModernizationNotFoundError when the aggregate was never saved."""
        ...

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None: ...
