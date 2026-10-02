from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(Exception):
    """RAISE EXCEPTION: periodo invalido (inicio > fim).

    Na rotina original este erro e lancado dentro do bloco BEGIN...EXCEPTION
    WHEN OTHERS, portanto e capturado pelo proprio handler e degrada para a
    linha de fallback; nunca propaga ao chamador.
    """

    def __init__(self, inicio: date | None, fim: date | None) -> None:
        super().__init__(f"Periodo invalido: inicio {inicio} > fim {fim}")
        self.inicio = inicio
        self.fim = fim


@dataclass(frozen=True)
class RelatorioMensalRow:
    """Linha do SETOF retornado pela funcao (colunas na ordem da tabela)."""

    mes_referencia: date | None
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal | None
    qtd_transacoes: int


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date | None,
    p_data_fim: date | None,
) -> list[RelatorioMensalRow]:
    """Port of sp_relatorio_mensal_cliente(bigint, date, date).

    The caller owns the transaction; this routine only uses a SAVEPOINT
    (begin_nested) to reproduce the PL/pgSQL BEGIN...EXCEPTION WHEN OTHERS
    block. The RAISE EXCEPTION for an inverted period is raised inside that
    protected block (as in the original), so it is caught by the handler and
    produces the degraded fallback row instead of propagating.
    """
    # BEGIN (L26) ... EXCEPTION WHEN OTHERS: everything below runs inside a
    # savepoint; on any error the savepoint is rolled back and the handler
    # (WARNING + fallback row) executes outside it, on a clean transaction.
    try:
        async with conn.begin_nested():
            # L27: IF p_data_inicio > p_data_fim (NULL-safe: in SQL,
            # NULL > NULL is NULL -> false, so the branch is skipped).
            if (
                p_data_inicio is not None
                and p_data_fim is not None
                and p_data_inicio > p_data_fim
            ):
                raise PeriodoInvalidoError(p_data_inicio, p_data_fim)

            # v_saldo_atual := fn_saldo_cliente(p_cliente_id)
            saldo_result = await conn.execute(
                text("SELECT fn_saldo_cliente(CAST(:cliente_id AS BIGINT)) AS saldo"),
                {"cliente_id": p_cliente_id},
            )
            v_saldo_atual: Decimal | None = saldo_result.scalar_one()

            # L32: RAISE NOTICE
            logger.info("Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual)

            main_sql = text(
                """
                WITH RECURSIVE meses AS (
                    SELECT DATE_TRUNC('month', CAST(:data_inicio AS DATE))::DATE AS mes
                    UNION ALL
                    SELECT (mes + INTERVAL '1 month')::DATE
                      FROM meses
                     WHERE mes < DATE_TRUNC('month', CAST(:data_fim AS DATE))
                ),
                movimento AS (
                    SELECT
                        DATE_TRUNC('month', t.data_transacao)::DATE AS mes,
                        SUM(CASE WHEN t.conta_destino_id IN (
                                SELECT id FROM contas WHERE cliente_id = CAST(:cliente_id AS BIGINT)
                            ) THEN t.valor ELSE 0 END) AS creditos,
                        SUM(CASE WHEN t.conta_origem_id IN (
                                SELECT id FROM contas WHERE cliente_id = CAST(:cliente_id AS BIGINT)
                            ) THEN t.valor ELSE 0 END) AS debitos,
                        COUNT(*) AS qtd
                      FROM transacoes t
                     WHERE t.status = 'EFETIVADA'
                       AND t.data_transacao >= CAST(:data_inicio AS DATE)
                       AND t.data_transacao <  CAST(:data_fim AS DATE) + INTERVAL '1 day'
                       AND (
                           t.conta_origem_id  IN (SELECT id FROM contas WHERE cliente_id = CAST(:cliente_id AS BIGINT))
                        OR t.conta_destino_id IN (SELECT id FROM contas WHERE cliente_id = CAST(:cliente_id AS BIGINT))
                       )
                     GROUP BY 1
                )
                SELECT
                    m.mes                                                    AS mes_referencia,
                    CAST(COALESCE(mv.creditos, 0) AS NUMERIC(18, 2))         AS total_creditos,
                    CAST(COALESCE(mv.debitos, 0) AS NUMERIC(18, 2))          AS total_debitos,
                    CAST(CAST(:saldo_atual AS NUMERIC)
                         + COALESCE(mv.creditos, 0)
                         - COALESCE(mv.debitos, 0) AS NUMERIC(18, 2))        AS saldo_consolidado,
                    CAST(COALESCE(mv.qtd, 0) AS INT)                         AS qtd_transacoes
                  FROM meses m
                  LEFT JOIN movimento mv ON mv.mes = m.mes
                 ORDER BY m.mes
                """
            )
            params: dict[str, object] = {
                "cliente_id": p_cliente_id,
                "data_inicio": p_data_inicio,
                "data_fim": p_data_fim,
                "saldo_atual": v_saldo_atual,
            }
            result = await conn.execute(main_sql, params)
            rows = result.fetchall()
    except Exception as exc:
        # L75: RAISE WARNING no handler; a transacao foi revertida ao savepoint.
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )
        fallback = await conn.execute(
            text(
                """
                SELECT
                    DATE_TRUNC('month', CAST(:data_inicio AS DATE))::DATE AS mes_referencia,
                    CAST(0 AS NUMERIC(18, 2))                              AS total_creditos,
                    CAST(0 AS NUMERIC(18, 2))                              AS total_debitos,
                    CAST(COALESCE(CAST(:saldo_atual AS NUMERIC), 0) AS NUMERIC(18, 2))
                                                                           AS saldo_consolidado,
                    CAST(0 AS INT)                                         AS qtd_transacoes
                """
            ),
            {"data_inicio": p_data_inicio, "saldo_atual": None},
        )
        rows = fallback.fetchall()

    return [
        RelatorioMensalRow(
            mes_referencia=row.mes_referencia,
            total_creditos=row.total_creditos,
            total_debitos=row.total_debitos,
            saldo_consolidado=row.saldo_consolidado,
            qtd_transacoes=row.qtd_transacoes,
        )
        for row in rows
    ]
