from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ParametroInvalidoError(Exception):
    """RAISE EXCEPTION equivalente para validação de parâmetros."""

    def __init__(self, p_dias: int | None) -> None:
        self.p_dias = p_dias
        super().__init__(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )


@dataclass(frozen=True)
class AtualizarStatusContasInativasResult:
    """Campos OUT da procedure, na ordem original."""

    p_afetadas: int


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int | None,
) -> AtualizarStatusContasInativasResult:
    """Marca contas ATIVA sem movimentação recente como INATIVA.

    Equivalente à procedure PL/pgSQL sp_atualizar_status_contas_inativas.
    A transação é de responsabilidade do chamador.
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
                       AND t.data_transacao >= NOW() - CAST(:p_dias AS INT) * INTERVAL '1 day'
               )
            """
        ),
        {"p_dias": p_dias},
    )
    p_afetadas: int = result.rowcount or 0

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

    return AtualizarStatusContasInativasResult(p_afetadas=p_afetadas)
