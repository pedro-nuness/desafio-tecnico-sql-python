"""Port of sp_atualizar_status_contas_inativas (PL/pgSQL procedure).

Marks accounts as INATIVA when they had no transactions (as origin or
destination) in the last ``p_dias`` days, then logs the batch inactivation
in log_auditoria.

The caller owns the transaction: no commit/rollback is issued here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ParametroInvalidoError(Exception):
    """Raised when p_dias is NULL or not positive (RAISE EXCEPTION)."""


@dataclass(frozen=True)
class ResultadoAtualizacaoStatus:
    """OUT parameters of the original procedure, in order."""

    p_afetadas: int


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int | None,
) -> ResultadoAtualizacaoStatus:
    """Inactivate accounts without recent transactions.

    Args:
        conn: Active async connection; the caller owns the transaction.
        p_dias: Number of days without transactions required to inactivate.

    Returns:
        ResultadoAtualizacaoStatus with the number of updated rows.

    Raises:
        ParametroInvalidoError: If p_dias is None or <= 0.
    """
    if p_dias is None or p_dias <= 0:
        raise ParametroInvalidoError(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )

    # Bulk UPDATE kept set-based in SQL. The interval is built with
    # make_interval + CAST so asyncpg can infer the bind type (rule 10).
    result = await conn.execute(
        text(
            """
            UPDATE contas c
               SET status = 'INATIVA'
             WHERE c.status = 'ATIVA'
               AND NOT EXISTS (
                    SELECT 1
                      FROM transacoes t
                     WHERE (t.conta_origem_id = c.id OR t.conta_destino_id = c.id)
                       AND t.data_transacao >= NOW() - make_interval(days => CAST(:p_dias AS INTEGER))
               )
            """
        ),
        {"p_dias": p_dias},
    )
    p_afetadas: int = result.rowcount

    logger.info(
        "INATIVACAO_LOTE: %d conta(s) inativada(s) apos %d dia(s) sem transacoes",
        p_afetadas,
        p_dias,
    )

    # jsonb_build_object: each value cast to its own type so the JSON keeps it.
    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'contas',
                'INATIVACAO_LOTE',
                jsonb_build_object(
                    'dias', CAST(:p_dias AS INTEGER),
                    'afetadas', CAST(:p_afetadas AS INTEGER)
                )
            )
            """
        ),
        {"p_dias": p_dias, "p_afetadas": p_afetadas},
    )

    return ResultadoAtualizacaoStatus(p_afetadas=p_afetadas)
