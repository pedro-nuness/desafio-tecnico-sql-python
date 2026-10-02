from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Sum of saldo over active accounts of a client, as NUMERIC(18,2)."""
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
    return result.scalar_one()
