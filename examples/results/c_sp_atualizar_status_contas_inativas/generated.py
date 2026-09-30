from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ParametroInvalidoError(ValueError):
    """Equivalente ao RAISE EXCEPTION da rotina legada."""

    def __init__(self, p_dias: int | None) -> None:
        super().__init__(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )
        self.p_dias = p_dias


@dataclass(frozen=True)
class ResultadoInativacao:
    """Substitui o parâmetro OUT p_afetadas."""

    afetadas: int


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int,
) -> ResultadoInativacao:
    """Marca contas ATIVA sem movimentação recente como INATIVA.

    Mantém o UPDATE e o INSERT de auditoria como SQL parametrizado;
    o chamador é dono da transação (nenhum commit/rollback aqui).
    """
    if p_dias is None or p_dias <= 0:
        raise ParametroInvalidoError(p_dias)

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
                       AND t.data_transacao >= NOW() - make_interval(days => :p_dias)
               )
            """
        ),
        {"p_dias": p_dias},
    )
    afetadas: int = result.rowcount

    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'contas',
                'INATIVACAO_LOTE',
                jsonb_build_object('dias', :p_dias, 'afetadas', :p_afetadas)
            )
            """
        ),
        {"p_dias": p_dias, "p_afetadas": afetadas},
    )

    logger.info(
        "Inativacao em lote concluida: dias=%s afetadas=%s", p_dias, afetadas
    )
    return ResultadoInativacao(afetadas=afetadas)
