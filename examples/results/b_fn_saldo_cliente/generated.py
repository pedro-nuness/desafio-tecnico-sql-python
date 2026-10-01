from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class SaldoClienteError(Exception):
    """Typed error replacing RAISE EXCEPTION in the original routine."""


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return the consolidated balance of all active accounts of a client.

    Python port of fn_saldo_cliente(p_cliente_id BIGINT) RETURNS NUMERIC(18,2).
    The aggregation stays in SQL (database_delegated); the caller owns the
    transaction.
    """
    result = await conn.execute(
        text(
            """
            SELECT CAST(COALESCE(SUM(saldo), 0) AS NUMERIC(18, 2))
              FROM contas
             WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
               AND status = 'ATIVA'
            """
        ),
        {"p_cliente_id": p_cliente_id},
    )
    total: Decimal | None = result.scalar_one()
    return Decimal("0.00") if total is None else total
