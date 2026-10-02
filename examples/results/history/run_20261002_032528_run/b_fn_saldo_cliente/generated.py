"""Python 3.14 port of PL/pgSQL function fn_saldo_cliente.

Returns the consolidated balance of all active accounts of a client.
"""

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return the total balance of active accounts for the given client.

    Mirrors the original NUMERIC(18,2) variable: the aggregate result is
    rounded to 2 decimal places (half away from zero) before being returned.
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
    v_total: Decimal = result.scalar_one()
    return v_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
