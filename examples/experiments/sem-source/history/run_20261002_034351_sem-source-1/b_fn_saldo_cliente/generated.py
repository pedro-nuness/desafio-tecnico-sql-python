from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Python port of PL/pgSQL function fn_saldo_cliente(bigint) -> numeric(18,2).

    Sums the balance of all active accounts ('ATIVA') for the given client,
    returning 0 when there are none (COALESCE preserved in SQL).
    """
    result = await conn.execute(
        text(
            """
            SELECT COALESCE(SUM(saldo), 0)
            FROM contas
            WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
              AND status = 'ATIVA'
            """
        ),
        {"p_cliente_id": p_cliente_id},
    )
    total: Decimal = result.scalar_one()
    return total.quantize(Decimal("0.01"))
