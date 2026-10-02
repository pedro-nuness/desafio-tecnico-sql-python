"""Python 3.14 port of PL/pgSQL function sp_relatorio_mensal_cliente.

Generates a monthly movement report for a client. Relational logic (recursive
CTE, aggregation, joins) stays in parameterized SQL; Python handles control
flow, logging and the degraded fallback path.

Faithful behaviour notes:
- In the original, the RAISE EXCEPTION for an inverted period is raised inside
  the block covered by the WHEN OTHERS handler, so it is swallowed and the
  degraded fallback row is returned (with v_saldo_atual still NULL -> 0).
- The period comparison with NULL dates evaluates to NULL (false) in
  PL/pgSQL, so no exception is raised and the main query runs, producing a
  single row with NULL mes_referencia.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(Exception):
    """RAISE EXCEPTION equivalent: invalid reporting period.

    Note: in the original routine this error is caught by the routine's own
    WHEN OTHERS handler, so callers of this port will normally NOT see it
    raised; it is raised internally and converted to the fallback row.
    """

    def __init__(self, inicio: date | None, fim: date | None) -> None:
        super().__init__(f"Periodo invalido: inicio {inicio} > fim {fim}")
        self.inicio = inicio
        self.fim = fim


@dataclass(frozen=True)
class RelatorioMensalRow:
    """One row of the SETOF result (columns in original order)."""

    mes_referencia: date | None
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


_RELATORIO_SQL = text(
    """
    WITH RECURSIVE meses AS (
        SELECT DATE_TRUNC('month', CAST(:p_data_inicio AS DATE))::DATE AS mes
        UNION ALL
        SELECT (mes + INTERVAL '1 month')::DATE
          FROM meses
         WHERE mes < DATE_TRUNC('month', CAST(:p_data_fim AS DATE))
    ),
    movimento AS (
        SELECT
            DATE_TRUNC('month', t.data_transacao)::DATE AS mes,
            SUM(CASE WHEN t.conta_destino_id IN (
                    SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
                ) THEN t.valor ELSE 0 END) AS creditos,
            SUM(CASE WHEN t.conta_origem_id IN (
                    SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
                ) THEN t.valor ELSE 0 END) AS debitos,
            COUNT(*) AS qtd
          FROM transacoes t
         WHERE t.status = 'EFETIVADA'
           AND t.data_transacao >= CAST(:p_data_inicio AS DATE)
           AND t.data_transacao <  CAST(:p_data_fim AS DATE) + INTERVAL '1 day'
           AND (
               t.conta_origem_id  IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT))
            OR t.conta_destino_id IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT))
           )
         GROUP BY 1
    )
    SELECT
        m.mes                                                    AS mes_referencia,
        COALESCE(mv.creditos, 0)                                 AS total_creditos,
        COALESCE(mv.debitos, 0)                                  AS total_debitos,
        CAST(:v_saldo_atual AS NUMERIC) + COALESCE(mv.creditos, 0)
                                      - COALESCE(mv.debitos, 0)  AS saldo_consolidado,
        COALESCE(mv.qtd, 0)::INT                                 AS qtd_transacoes
      FROM meses m
      LEFT JOIN movimento mv ON mv.mes = m.mes
      ORDER BY m.mes
    """
)

_FALLBACK_SQL = text(
    """
    SELECT
        DATE_TRUNC('month', CAST(:p_data_inicio AS DATE))::DATE AS mes_referencia,
        0::NUMERIC(18,2)                                        AS total_creditos,
        0::NUMERIC(18,2)                                        AS total_debitos,
        COALESCE(CAST(:v_saldo_atual AS NUMERIC), 0)            AS saldo_consolidado,
        0::INT                                                  AS qtd_transacoes
    """
)

_SALDO_SQL = text(
    "SELECT fn_saldo_cliente(CAST(:p_cliente_id AS BIGINT)) AS saldo"
)


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date | None,
    p_data_fim: date | None,
) -> list[RelatorioMensalRow]:
    """Generate the monthly movement report for a client.

    The caller owns the transaction. The protected section (validation,
    balance lookup and main query) runs inside a nested transaction
    (savepoint) mirroring the original BEGIN ... EXCEPTION WHEN OTHERS END
    block: any error there is logged as a warning and the degraded fallback
    row is returned instead of propagating.
    """
    v_saldo_atual: Decimal | None = None

    try:
        async with conn.begin_nested():
            # IF p_data_inicio > p_data_fim: in SQL, NULL comparison yields
            # NULL (false), so NULL dates skip the check instead of raising.
            if (
                p_data_inicio is not None
                and p_data_fim is not None
                and p_data_inicio > p_data_fim
            ):
                raise PeriodoInvalidoError(p_data_inicio, p_data_fim)

            saldo_row = (
                await conn.execute(_SALDO_SQL, {"p_cliente_id": p_cliente_id})
            ).one()
            v_saldo_atual: Decimal | None = saldo_row.saldo
            logger.info(
                "Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual
            )

            params = {
                "p_cliente_id": p_cliente_id,
                "p_data_inicio": p_data_inicio,
                "p_data_fim": p_data_fim,
                "v_saldo_atual": v_saldo_atual,
            }
            rows = (await conn.execute(_RELATORIO_SQL, params)).all()
    except Exception as exc:  # WHEN OTHERS: swallow and degrade, as in the original
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )
        fallback = (
            await conn.execute(
                _FALLBACK_SQL,
                {
                    "p_data_inicio": p_data_inicio,
                    "v_saldo_atual": v_saldo_atual,
                },
            )
        ).one()
        return [
            RelatorioMensalRow(
                mes_referencia=fallback.mes_referencia,
                total_creditos=fallback.total_creditos,
                total_debitos=fallback.total_debitos,
                saldo_consolidado=fallback.saldo_consolidado,
                qtd_transacoes=fallback.qtd_transacoes,
            )
        ]

    return [
        RelatorioMensalRow(
            mes_referencia=r.mes_referencia,
            total_creditos=r.total_creditos,
            total_debitos=r.total_debitos,
            saldo_consolidado=r.saldo_consolidado,
            qtd_transacoes=r.qtd_transacoes,
        )
        for r in rows
    ]
