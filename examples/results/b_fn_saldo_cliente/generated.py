"""Modernized port of PL/pgSQL function fn_saldo_cliente.

Returns the consolidated balance of all active accounts ('ATIVA')
belonging to a given client.

The caller owns the transaction: no commit/rollback is performed here.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return the summed balance of the client's active accounts.

    Aggregation stays in the database (set-based SQL, parameterized).
    Mirrors: SELECT COALESCE(SUM(saldo), 0) FROM contas
             WHERE cliente_id = :p_cliente_id AND status = 'ATIVA'.
    """
    result = await conn.execute(
        text(
            """
            SELECT COALESCE(SUM(saldo), 0)
              FROM contas
             WHERE cliente_id = :p_cliente_id
               AND status = 'ATIVA'
            """
        ),
        {"p_cliente_id": p_cliente_id},
    )
    row = result.one()
    return Decimal(row[0])
