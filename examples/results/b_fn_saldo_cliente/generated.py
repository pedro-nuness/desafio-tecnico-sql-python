"""Python port of PL/pgSQL function fn_saldo_cliente.

Original:
    CREATE OR REPLACE FUNCTION fn_saldo_cliente(p_cliente_id BIGINT)
    RETURNS NUMERIC(18,2)

Returns the consolidated balance over all active accounts ('ATIVA')
of a given client. Pure read-only aggregation delegated to PostgreSQL;
the caller owns the transaction.
"""

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class FnSaldoClienteError(Exception):
    """Typed replacement for RAISE EXCEPTION in fn_saldo_cliente."""


async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int) -> Decimal:
    """Return the summed balance of the client's active accounts.

    Mirrors the original NUMERIC(18,2) declaration: the SQL casts the
    aggregated value so the driver yields a Decimal rounded to 2 places,
    matching the PL/pgSQL variable assignment semantics.
    """
    logger.debug("fn_saldo_cliente: computing balance for cliente_id=%d", p_cliente_id)

    stmt = text(
        """
        SELECT CAST(COALESCE(SUM(saldo), 0) AS NUMERIC(18, 2))
          FROM contas
         WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
           AND status = 'ATIVA'
        """
    )
    result = await conn.execute(stmt, {"p_cliente_id": p_cliente_id})
    row = result.one()
    total: Decimal | None = row[0]
    # Defensive: COALESCE guarantees non-null, but keep typing honest.
    return total if total is not None else Decimal("0")
