from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(Exception):
    """RAISE EXCEPTION 'Periodo invalido: inicio % > fim %'."""

    def __init__(self, inicio: date, fim: date) -> None:
        super().__init__(f"Periodo invalido: inicio {inicio} > fim {fim}")
        self.inicio = inicio
        self.fim = fim


@dataclass(frozen=True)
class RelatorioMensalRow:
    """Row of the SETOF table returned by the routine (columns in order)."""

    mes_referencia: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


def _q18_2(value: Decimal) -> Decimal:
    """Reproduce NUMERIC(18,2) rounding on assignment (half away from zero)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _obter_saldo_atual(conn: AsyncConnection, p_cliente_id: int) -> Decimal | None:
    """v_saldo_atual := fn_saldo_cliente(p_cliente_id); kept in the database."""
    result = await conn.execute(
        text("SELECT fn_saldo_cliente(CAST(:cliente_id AS BIGINT))"),
        {"cliente_id": p_cliente_id},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return _q18_2(Decimal(row[0]))


async def _query_relatorio(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date,
    p_data_fim: date,
    v_saldo_atual: Decimal | None,
) -> list[RelatorioMensalRow]:
    """The recursive-CTE report query, kept set-based in SQL."""
    sql = text(
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
            m.mes                                       AS mes_referencia,
            COALESCE(mv.creditos, 0)                    AS total_creditos,
            COALESCE(mv.debitos,  0)                    AS total_debitos,
            CAST(:saldo_atual AS NUMERIC) + COALESCE(mv.creditos, 0)
                          - COALESCE(mv.debitos,  0)    AS saldo_consolidado,
            COALESCE(mv.qtd, 0)::INT                    AS qtd_transacoes
          FROM meses m
          LEFT JOIN movimento mv ON mv.mes = m.mes
          ORDER BY m.mes
        """
    )
    params = {
        "cliente_id": p_cliente_id,
        "data_inicio": p_data_inicio,
        "data_fim": p_data_fim,
        "saldo_atual": v_saldo_atual if v_saldo_atual is not None else 0,
    }
    result = await conn.execute(sql, params)
    rows: list[RelatorioMensalRow] = []
    for row in result.mappings():
        rows.append(
            RelatorioMensalRow(
                mes_referencia=row["mes_referencia"],
                total_creditos=_q18_2(Decimal(row["total_creditos"])),
                total_debitos=_q18_2(Decimal(row["total_debitos"])),
                saldo_consolidado=_q18_2(Decimal(row["saldo_consolidado"])),
                qtd_transacoes=int(row["qtd_transacoes"]),
            )
        )
    return rows


async def _fallback_row(
    conn: AsyncConnection,
    p_data_inicio: date,
    v_saldo_atual: Decimal | None,
) -> list[RelatorioMensalRow]:
    """Degraded fallback row of the WHEN OTHERS handler."""
    sql = text(
        """
        SELECT
            DATE_TRUNC('month', CAST(:data_inicio AS DATE))::DATE AS mes_referencia,
            0::NUMERIC(18,2) AS total_creditos,
            0::NUMERIC(18,2) AS total_debitos,
            COALESCE(CAST(:saldo_atual AS NUMERIC), 0) AS saldo_consolidado,
            0::INT AS qtd_transacoes
        """
    )
    result = await conn.execute(
        sql,
        {"data_inicio": p_data_inicio, "saldo_atual": v_saldo_atual},
    )
    row = result.mappings().one()
    return [
        RelatorioMensalRow(
            mes_referencia=row["mes_referencia"],
            total_creditos=Decimal(row["total_creditos"]),
            total_debitos=Decimal(row["total_debitos"]),
            saldo_consolidado=Decimal(row["saldo_consolidado"]),
            qtd_transacoes=int(row["qtd_transacoes"]),
        )
    ]


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date,
    p_data_fim: date,
) -> list[RelatorioMensalRow]:
    """Python port of sp_relatorio_mensal_cliente(p_cliente_id, p_data_inicio, p_data_fim).

    The caller owns the transaction. The original WHEN OTHERS handler is a
    savepoint: the protected statements (saldo lookup + report query) run
    inside begin_nested() so the transaction is not left aborted when they
    fail, and the fallback row is returned instead of raising.
    """
    v_saldo_atual: Decimal | None = None

    # The original validation RAISE sits inside the block whose WHEN OTHERS
    # catches it, so an invalid period degrades to the fallback row instead
    # of propagating an exception. Preserve that behaviour.
    try:
        async with conn.begin_nested():
            if p_data_inicio > p_data_fim:
                raise PeriodoInvalidoError(p_data_inicio, p_data_fim)

            v_saldo_atual = await _obter_saldo_atual(conn, p_cliente_id)
            logger.info(
                "Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual
            )

            return await _query_relatorio(
                conn, p_cliente_id, p_data_inicio, p_data_fim, v_saldo_atual
            )
    except Exception as exc:
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )
        return await _fallback_row(conn, p_data_inicio, v_saldo_atual)
