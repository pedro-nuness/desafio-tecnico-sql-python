"""Modernized version of fn_saldo_cliente.

Returns the consolidated balance of all active accounts of a client.
The aggregation stays in the database (database_delegated strategy).
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class SaldoCliente:
    """Return value of fn_saldo_cliente (numeric(18, 2))."""

    total: float


async def fn_saldo_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
) -> SaldoCliente:
    """Return the sum of balances of all active accounts of a client.

    Args:
        conn: Async database connection (caller owns the transaction).
        p_cliente_id: Client identifier.

    Returns:
        SaldoCliente with the consolidated balance (0 if no active accounts).
    """
    result: Result = await conn.execute(
        text(
            """
            SELECT COALESCE(SUM(saldo), 0) AS total
              FROM contas
             WHERE cliente_id = :p_cliente_id
               AND status = 'ATIVA'
            """
        ),
        {"p_cliente_id": p_cliente_id},
    )
    row = result.one()
    return SaldoCliente(total=float(row.total))
