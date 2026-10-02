"""Python 3.14 port of PL/pgSQL procedure sp_atualizar_status_contas_inativas.

Marks accounts as 'INATIVA' when they had no transactions in the last
``p_dias`` days and writes an audit log entry. The caller owns the transaction.
"""

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger: Final[logging.Logger] = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpAtualizarStatusContasInativasResult:
    """OUT parameters of the original procedure, in declaration order."""

    p_afetadas: int


class ParametroInvalidoError(ValueError):
    """RAISE EXCEPTION equivalent: invalid input parameter."""


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int,
) -> SpAtualizarStatusContasInativasResult:
    """Inactivate accounts without recent transactions.

    Mirrors the original procedure: validates ``p_dias``, runs one set-based
    UPDATE, captures rowcount (GET DIAGNOSTICS ROW_COUNT) and inserts an audit
    log row with a jsonb payload.
    """
    if p_dias is None or p_dias <= 0:
        raise ParametroInvalidoError(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )

    update_result = await conn.execute(
        text(
            """
            UPDATE contas c
               SET status = 'INATIVA'
             WHERE c.status = 'ATIVA'
               AND NOT EXISTS (
                    SELECT 1
                      FROM transacoes t
                     WHERE (t.conta_origem_id = c.id OR t.conta_destino_id = c.id)
                       AND t.data_transacao >= NOW()
                                        - make_interval(days => CAST(:p_dias AS INTEGER))
               )
            """
        ),
        {"p_dias": p_dias},
    )
    p_afetadas: int = update_result.rowcount or 0

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

    logger.info(
        "INATIVACAO_LOTE executada: dias=%s, afetadas=%s", p_dias, p_afetadas
    )
    return SpAtualizarStatusContasInativasResult(p_afetadas=p_afetadas)
