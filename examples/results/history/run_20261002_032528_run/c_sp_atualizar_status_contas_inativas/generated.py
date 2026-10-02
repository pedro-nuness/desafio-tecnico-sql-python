"""Python 3.14 port of PL/pgSQL procedure sp_atualizar_status_contas_inativas.

Marks accounts as INATIVA when they have no transactions in the last p_dias days
and returns the number of affected accounts.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ParametroInvalidoError(Exception):
    """Raised when p_dias is NULL or non-positive (RAISE EXCEPTION equivalent)."""


@dataclass(frozen=True)
class AtualizarStatusContasInativasResult:
    """OUT parameters of the original procedure, in order."""

    p_afetadas: int


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int | None,
) -> AtualizarStatusContasInativasResult:
    """Inactivate accounts without transactions in the last ``p_dias`` days.

    The caller owns the transaction: no commit/rollback is issued here.
    """
    if p_dias is None or p_dias <= 0:
        raise ParametroInvalidoError(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )

    result: Any = await conn.execute(
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

    return AtualizarStatusContasInativasResult(p_afetadas=p_afetadas)
