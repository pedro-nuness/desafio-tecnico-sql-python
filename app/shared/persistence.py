"""Transaction boundary port, generic to every feature (no database library here).

Use cases decide where a transaction starts and ends; repositories are injected on their own
and work inside the transaction in progress:

    async with transactions.transaction() as tx:
        running = await modernizations.find_by_id(execution_id)
        await modernizations.update(running.complete(outcome))
        await tx.commit()          # explicit: leaving without commit() rolls back
"""

from contextlib import AbstractAsyncContextManager
from typing import Protocol


class Transaction(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class TransactionManager(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """Opens a transaction for the current task. Nothing is persisted without commit()."""
        ...
