"""Python 3.14 port of PL/pgSQL function fn_saldo_cliente.

Returns the consolidated balance of all active accounts of a client.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return SUM(saldo) of active accounts for p_cliente_id, as NUMERIC(18,2)."""
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
    total: Decimal = result.scalar_one()
    # PL/pgSQL variable v_total is NUMERIC(18,2): rounded to 2 places on assignment.
    return total.quantize(Decimal("0.01"))
