"""The evaluation database: throwaway schemas, and the final rows of their tables.

Only a disposable database (EVALUATION_DATABASE_URL), never the application's: the generated
code runs against it.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.features.modernization.validation.checks.behavior.comparison import canonical
from app.shared.errors import AppError


class Sandbox:
    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

    @asynccontextmanager
    async def schema(self, setup_sql: str) -> AsyncIterator[str]:
        """A throwaway schema with the scenario's tables, its seed and the routines."""
        schema = f"eval_{uuid4().hex}"
        await self._run_script(
            f'CREATE SCHEMA "{schema}"; SET search_path TO "{schema}";\n{setup_sql}'
        )
        try:
            yield schema
        finally:
            await self._run_script(f'DROP SCHEMA "{schema}" CASCADE')

    @asynccontextmanager
    async def connect(self, schema: str) -> AsyncIterator[AsyncConnection]:
        """A connection on the schema, closed without commit: what the call wrote is undone."""
        async with self._engine_or_fail().connect() as conn:
            await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            yield conn

    async def tables(self, schema: str) -> tuple[str, ...]:
        """Every table the setup created (the default of compare_tables)."""
        async with self._engine_or_fail().connect() as conn:
            result = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema ORDER BY 1"),
                {"schema": schema},
            )
            return tuple(result.scalars())

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def _run_script(self, script: str) -> None:
        # Multi-statement scripts (DDL, $$ bodies) need the driver's simple query protocol.
        async with self._engine_or_fail().connect() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(script)  # type: ignore[union-attr]

    def _engine_or_fail(self) -> AsyncEngine:
        if self._database_url is None:
            raise AppError("Evaluation database not configured: set EVALUATION_DATABASE_URL")
        if self._engine is None:
            # NullPool: every connection is fresh, so a session setting never leaks.
            self._engine = create_async_engine(self._database_url, poolclass=NullPool)
        return self._engine


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The final rows of the compared tables, taken the same way on both sides."""

    tables: tuple[str, ...]
    ignore_columns: tuple[str, ...]

    async def take(self, conn: AsyncConnection) -> dict[str, tuple[str, ...]]:
        state: dict[str, tuple[str, ...]] = {}
        for table in self.tables:
            quoted = conn.dialect.identifier_preparer.quote(table)  # caller's name, quoted
            result = await conn.execute(
                # ::text: parsed here with Decimal, so NUMERIC values never go through float.
                text(f"SELECT (to_jsonb(t) - CAST(:ignored AS text[]))::text FROM {quoted} AS t"),  # noqa: S608
                {"ignored": list(self.ignore_columns)},
            )
            state[table] = tuple(
                sorted(
                    canonical(json.loads(raw, parse_float=Decimal, parse_int=Decimal))
                    for (raw,) in result.all()
                )
            )
        return state


def sqlstate(exc: BaseException) -> str:
    """PostgreSQL error code of the driver error in the cause chain ("" when none)."""
    current: BaseException | None = exc
    while current is not None:
        if code := getattr(current, "sqlstate", None):
            return str(code)
        current = current.__cause__
    return ""
