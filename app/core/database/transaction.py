"""SQLAlchemy implementation of the TransactionManager port (app/shared/persistence.py).

`transaction()` opens one AsyncSession and makes it the *current* session of the running
asyncio task (a ContextVar: concurrent requests never share a session). Repositories ask for
it with `current_session()`, so they are injected like any other dependency instead of being
built per transaction. Commit is explicit; leaving the block without commit() rolls back.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Module level, as contextvars recommends. Holds the manager too, so a repository can never
# pick up a session opened by another database's manager.
_active: ContextVar[tuple[SessionTransactionManager, AsyncSession] | None] = ContextVar(
    "active_database_transaction", default=None
)


class SessionTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class SessionTransactionManager:
    """One per database (process-wide); each `transaction()` is short-lived."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SessionTransaction]:
        if _active.get() is not None:
            raise RuntimeError("Nested transactions are not supported")
        session = self._session_factory()
        token = _active.set((self, session))
        try:
            yield SessionTransaction(session)
        finally:
            _active.reset(token)
            # Anything not explicitly committed is discarded.
            await session.rollback()
            await session.close()

    def current_session(self) -> AsyncSession:
        active = _active.get()
        if active is None or active[0] is not self:
            raise RuntimeError(
                "Repository used outside a transaction: wrap the call in "
                "`async with transactions.transaction() as tx:`"
            )
        return active[1]
