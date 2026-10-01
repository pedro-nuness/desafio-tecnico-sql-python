"""core/database/transaction.py: the current-session transaction shared by every feature."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.transaction import SessionTransactionManager


def _manager() -> tuple[SessionTransactionManager, list[MagicMock]]:
    sessions: list[MagicMock] = []

    def factory() -> MagicMock:
        session = MagicMock(spec=AsyncSession)
        session.commit, session.rollback, session.close = AsyncMock(), AsyncMock(), AsyncMock()
        sessions.append(session)
        return session

    return SessionTransactionManager(factory), sessions  # type: ignore[arg-type]


async def test_repositories_see_the_session_of_the_transaction_in_progress() -> None:
    manager, sessions = _manager()

    async with manager.transaction():
        assert manager.current_session() is sessions[0]


async def test_leaving_without_commit_rolls_back_and_closes() -> None:
    manager, sessions = _manager()

    async with manager.transaction():
        pass

    [session] = sessions
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_commit_is_explicit() -> None:
    manager, sessions = _manager()

    async with manager.transaction() as tx:
        await tx.commit()

    sessions[0].commit.assert_awaited_once()
    sessions[0].close.assert_awaited_once()


async def test_an_error_inside_the_block_rolls_back_and_propagates() -> None:
    manager, sessions = _manager()

    with pytest.raises(ValueError, match="boom"):
        async with manager.transaction():
            raise ValueError("boom")

    sessions[0].rollback.assert_awaited_once()
    with pytest.raises(RuntimeError):
        manager.current_session()  # the session no longer leaks after the block


def test_repositories_fail_loudly_outside_a_transaction() -> None:
    manager, _ = _manager()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        manager.current_session()


async def test_nested_transactions_are_rejected() -> None:
    manager, _ = _manager()

    async with manager.transaction():
        with pytest.raises(RuntimeError, match="Nested transactions"):
            async with manager.transaction():
                pass


async def test_concurrent_tasks_get_their_own_session() -> None:
    manager, _ = _manager()
    seen: list[AsyncSession] = []
    both_open = asyncio.Barrier(2)

    async def use_case() -> None:
        async with manager.transaction():
            await both_open.wait()  # both transactions open at the same time
            seen.append(manager.current_session())

    await asyncio.gather(use_case(), use_case())

    assert len(seen) == 2 and seen[0] is not seen[1]


async def test_a_session_of_another_manager_is_never_picked_up() -> None:
    first, _ = _manager()
    second, _ = _manager()

    async with first.transaction():
        with pytest.raises(RuntimeError, match="outside a transaction"):
            second.current_session()
