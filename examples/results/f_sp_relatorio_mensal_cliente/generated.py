"""Modernization of sp_relatorio_mensal_cliente (PL/pgSQL -> Python 3.14).

Relational work (recursive month CTE, aggregations, joins) stays in SQL.
Python handles validation, orchestration, logging and degraded-mode fallback.
The caller owns the transaction; no COMMIT/ROLLBACK is issued here.
"""

import datetime as dt
import decimal
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class RelatorioError(Exception):
    """Maps PL/pgSQL RAISE EXCEPTION."""

    def __init__(self, mensagem: str) -> None:
        super().__init__(mensagem)
        self.message = mensagem


@dataclass(frozen=True, slots=True)
class LinhaRelatorioMensal:
    """Row returned by the monthly client movement report."""

    mes_referencia: dt.date
    total_creditos: decimal.Decimal
    total_debitos: decimal.Decimal
    saldo_consolidado: decimal.Decimal | None
    qtd_transacoes: int


_MAIN_REPORT_SQL = text("""
WITH RECURSIVE meses AS (
    SELECT DATE_TRUNC('month', :p_data_inicio)::DATE AS mes
    UNION ALL
    SELECT (mes + INTERVAL '1 month')::DATE
      FROM meses
     WHERE mes < DATE_TRUNC('month', :p_data_fim)
),
movimento AS (
    SELECT
        DATE_TRUNC('month', t.data_transacao)::DATE AS mes,
        SUM(CASE WHEN t.conta_destino_id IN (
                SELECT id FROM contas WHERE cliente_id = :p_cliente_id
            ) THEN t.valor ELSE 0 END) AS creditos,
        SUM(CASE WHEN t.conta_origem_id IN (
                SELECT id FROM contas WHERE cliente_id = :p_cliente_id
            ) THEN t.valor ELSE 0 END) AS debitos,
        COUNT(*) AS qtd
      FROM transacoes t
     WHERE t.status = 'EFETIVADA'
       AND t.data_transacao >= :p_data_inicio
       AND t.data_transacao <  (:p_data_fim + INTERVAL '1 day')
       AND (
           t.conta_origem_id  IN (SELECT id FROM contas WHERE cliente_id = :p_cliente_id)
        OR t.conta_destino_id IN (SELECT id FROM contas WHERE cliente_id = :p_cliente_id)
       )
     GROUP BY 1
)
SELECT
    m.mes                                        AS mes_referencia,
    COALESCE(mv.creditos, 0)                     AS total_creditos,
    COALESCE(mv.debitos, 0)                      AS total_debitos,
    :v_saldo_atual + COALESCE(mv.creditos, 0)
                 - COALESCE(mv.debitos, 0)       AS saldo_consolidado,
    COALESCE(mv.qtd, 0)::INT                     AS qtd_transacoes
  FROM meses m
  LEFT JOIN movimento mv ON mv.mes = m.mes
 ORDER BY m.mes
""")

_FALLBACK_ROW_SQL = text("""
SELECT
    DATE_TRUNC('month', :p_data_inicio)::DATE AS mes_referencia,
    0::NUMERIC(18,2)                          AS total_creditos,
    0::NUMERIC(18,2)                          AS total_debitos,
    COALESCE(:v_saldo_atual, 0)               AS saldo_consolidado,
    0::INT                                    AS qtd_transacoes
""")

_SALDO_ATUAL_SQL = text(
    "SELECT fn_saldo_cliente(:p_cliente_id) AS saldo"
)


async def _obter_saldo_atual(conn: AsyncConnection, cliente_id: int) -> decimal.Decimal | None:
    """Calls legacy fn_saldo_cliente (kept in DB per external-routine rule)."""
    resultado = await conn.execute(_SALDO_ATUAL_SQL, {"p_cliente_id": cliente_id})
    return resultado.scalar_one_or_none()


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: dt.date,
    p_data_fim: dt.date,
) -> list[LinhaRelatorioMensal]:
    """Generates the monthly movement report for a client.

    Mirrors the legacy PL/pgSQL behavior including the degraded fallback row.
    Transaction ownership belongs to the caller.
    """
    if p_data_inicio > p_data_fim:
        msg = f"Periodo invalido: inicio {p_data_inicio} > fim {p_data_fim}"
        raise RelatorioError(msg)

    params_base: dict[str, object] = {
        "p_cliente_id": p_cliente_id,
        "p_data_inicio": p_data_inicio,
        "p_data_fim": p_data_fim,
    }

    try:
        v_saldo_atual = await _obter_saldo_atual(conn, p_cliente_id)
        logger.info("Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual)

        params = {**params_base, "v_saldo_atual": v_saldo_atual}
        resultado = await conn.execute(_MAIN_REPORT_SQL, params)
        linhas: list[LinhaRelatorioMensal] = [
            LinhaRelatorioMensal(
                mes_referencia=row[0],
                total_creditos=row[1],
                total_debitos=row[2],
                saldo_consolidado=row[3],
                qtd_transacoes=int(row[4]),
            )
            for row in resultado.fetchall()
        ]
        return linhas
    except Exception as exc:  # noqa: BLE001 -- mirrors legacy WHEN OTHERS handler
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.",
            exc,
        )
        resultado_fb = await conn.execute(
            _FALLBACK_ROW_SQL,
            {"p_data_inicio": p_data_inicio, "v_saldo_atual": v_saldo_atual},
        )
        fb_row = resultado_fb.fetchone()
        assert fb_row is not None  # single-row literal SELECT always yields one row
        return [
            LinhaRelatorioMensal(
                mes_referencia=fb_row[0],
                total_creditos=fb_row[1],
                total_debitos=fb_row[2],
                saldo_consolidado=fb_row[3],
                qtd_transacoes=int(fb_row[4]),
            )
        ]
