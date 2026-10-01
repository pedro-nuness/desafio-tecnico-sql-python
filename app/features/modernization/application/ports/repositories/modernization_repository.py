from typing import Protocol
from uuid import UUID

from app.features.modernization.domain.models.modernization import Modernization


class ModernizationRepository(Protocol):
    """Persistence of Modernization aggregates.

    Works inside the transaction opened by the caller (app.shared.persistence); flushes,
    never commits: the caller does.
    """

    async def save(self, modernization: Modernization) -> None: ...

    async def update(self, modernization: Modernization) -> None:
        """Raises app.shared.errors.NotFoundError when the aggregate was never saved."""
        ...

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None: ...
