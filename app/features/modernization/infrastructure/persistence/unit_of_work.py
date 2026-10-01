from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.features.modernization.infrastructure.persistence.repositories.sqlalchemy_modernization_repository import (  # noqa: E501
    SqlAlchemyModernizationRepository,
)


class SqlAlchemyUnitOfWork:
    """One AsyncSession (= one transaction) shared by every repository.

    Future aggregates plug in here, e.g. `self.evaluations = SqlAlchemyEvaluationRepository(
    self._session)`, without touching the existing repositories.
    """

    modernizations: SqlAlchemyModernizationRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.modernizations = SqlAlchemyModernizationRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            # Anything not explicitly committed is discarded.
            await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self._require_session().commit()

    async def rollback(self) -> None:
        await self._require_session().rollback()

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("SqlAlchemyUnitOfWork used outside 'async with'")
        return self._session
