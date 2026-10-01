"""Port of the PL/pgSQL function fn_saldo_cliente.

Returns the consolidated balance of all active accounts of a client.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return SUM(saldo) of active accounts for the given client (0 if none).

    The aggregation is kept in SQL (database_delegated). The CAST to
    NUMERIC(18,2) reproduces the rounding that the PL/pgSQL variable
    declaration applied on assignment into v_total.
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
    row = result.one()
    return Decimal(row[0])
