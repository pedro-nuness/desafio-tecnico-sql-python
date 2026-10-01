from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ParametroInvalidoError(Exception):
    """RAISE EXCEPTION equivalente para p_dias invalido."""


@dataclass(frozen=True)
class ResultadoAtualizacaoStatus:
    """OUT parameters, in order."""

    p_afetadas: int


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int | None,
) -> ResultadoAtualizacaoStatus:
    """Marca contas ATIVA sem movimentacao recente como INATIVA.

    O caller owns the transaction: nenhuma commit/rollback aqui.
    """
    if p_dias is None or p_dias <= 0:
        raise ParametroInvalidoError(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )

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
                       AND t.data_transacao >= NOW() - CAST(:p_dias AS INT) * INTERVAL '1 day'
               )
            """
        ),
        {"p_dias": p_dias},
    )
    p_afetadas = result.rowcount

    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'contas',
                'INATIVACAO_LOTE',
                jsonb_build_object(
                    'dias', CAST(:p_dias AS INT),
                    'afetadas', CAST(:p_afetadas AS INT)
                )
            )
            """
        ),
        {"p_dias": p_dias, "p_afetadas": p_afetadas},
    )

    return ResultadoAtualizacaoStatus(p_afetadas=p_afetadas)
