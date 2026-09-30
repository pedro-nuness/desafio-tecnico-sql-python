"""Modernization of sp_relatorio_mensal_cliente (PL/pgSQL -> Python 3.14).

Relational logic (recursive CTE, aggregations, joins) stays in parameterized SQL.
The Python layer handles validation, orchestration, logging and the degraded-mode
fallback previously implemented via the PL/pgSQL EXCEPTION block."""

import datetime as dt
import decimal
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(ValueError):
    """Raised when p_data_inicio > p_data_fim (was RAISE EXCEPTION)."""


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelatorioMensalRow:
    """One monthly summary line (mirrors RETURNS TABLE columns)."""

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
        m.mes                                    AS mes_referencia,
        COALESCE(mv.creditos, 0)                 AS total_creditos,
        COALESCE(mv.debitos, 0)                  AS total_debitos,
        :v_saldo_atual + COALESCE(mv.creditos, 0)
                       - COALESCE(mv.debitos, 0) AS saldo_consolidado,
        COALESCE(mv.qtd, 0)::INT                 AS qtd_transacoes
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


async def _fetch_saldo_atual(conn: AsyncConnection, cliente_id: int) -> decimal.Decimal | None:
    """Call legacy fn_saldo_cliente (kept in DB: external routine dependency)."""
    result = await conn.execute(_SALDO_ATUAL_SQL, {"p_cliente_id": cliente_id})
    value = result.scalar_one_or_none()
    return decimal.Decimal(value) if value is not None else None


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: dt.date,
    p_data_fim: dt.date,
) -> list[RelatorioMensalRow]:
    """Generate the client's monthly movement report.

    Returns one row per month between p_data_inicio and p_data_fim (inclusive,
    truncated to months). On unexpected failure, logs a warning and returns a
    single degraded fallback row — mirroring the original WHEN OTHERS handler.

    The caller owns the transaction; this function never commits or rolls back.
    Read-only routine: no row locking required.
    """
    # Validation equivalent to: IF p_data_inicio > p_data_fim THEN RAISE EXCEPTION ...
    if p_data_inicio > p_data_fim:
        msg = f"Periodo invalido: inicio {p_data_inicio} > fim {p_data_fim}"
        raise PeriodoInvalidoError(msg)

    try:
        v_saldo_atual = await _fetch_saldo_atual(conn, p_cliente_id)
        logger.info("Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual)

        params = {
            "p_cliente_id": p_cliente_id,
            "p_data_inicio": p_data_inicio,
            "p_data_fim": p_data_fim,
            "v_saldo_atual": v_saldo_atual if v_saldo_atual is not None else 0,
        }
        result = await conn.execute(_MAIN_REPORT_SQL, params)
        rows = [
            RelatorioMensalRow(
                mes_referencia=row.mes_referencia,
                total_creditos=row.total_creditos,
                total_debitos=row.total_debitos,
                saldo_consolidado=(
                    decimal.Decimal(row.saldo_consolidado)
                    if row.saldo_consolidado is not None
                    else None
                ),
                qtd_transacoes=int(row.qtd_transacoes),
            )
            for row in result.mappings()
        ]
        return rows
    except Exception as exc:  # noqa: BLE001 — mirrors WHEN OTHERS degraded mode
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )
        fb_result = await conn.execute(
            _FALLBACK_ROW_SQL,
            {"p_data_inicio": p_data_inicio, "v_saldo_atual": v_saldo_atual},
        )
        fb_row = fb_result.one()
        return [
            RelatorioMensalRow(
                mes_referencia=fb_row.mes_referencia,
                total_creditos=decimal.Decimal(fb_row.total_creditos),
                total_debitos=decimal.Decimal(fb_row.total_debitos),
                saldo_consolidado=(
                    decimal.Decimal(fb_row.saldo_consolidado)
                    if fb_row.saldo_consolidado is not None
                    else None
                ),
                qtd_transacoes=int(fb_row.qtd_transacoes),
            )
        ]
