"""Python 3.14 port of PL/pgSQL function sp_relatorio_mensal_cliente.

Generates a monthly transaction report for a client. Relational logic
(recursive month CTE, aggregation, join) stays in SQL; Python handles
control flow, logging and the degraded fallback path.

Faithful to the original control flow: the whole body (including the
period validation RAISE EXCEPTION) lives inside one block whose
WHEN OTHERS handler swallows any error and returns a single fallback
row. The typed exception is raised, then caught by the handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class RelatorioMensalError(Exception):
    """Base error for sp_relatorio_mensal_cliente."""


class PeriodoInvalidoError(RelatorioMensalError):
    """Maps the original RAISE EXCEPTION for an inverted period.

    In the original this error is immediately caught by the block-level
    WHEN OTHERS handler, which logs a warning and returns the fallback
    row; it is therefore raised here and caught by the same handler.
    """


@dataclass(frozen=True, slots=True)
class RelatorioMensalRow:
    """One row of the SETOF table returned by the original function.

    Columns in original order: mes_referencia, total_creditos,
    total_debitos, saldo_consolidado, qtd_transacoes.
    """

    mes_referencia: date | None
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


async def _fetch_saldo_atual(conn: AsyncConnection, p_cliente_id: int) -> Decimal | None:
    """v_saldo_atual := fn_saldo_cliente(p_cliente_id).

    The external routine dependency is kept in SQL.
    """
    result = await conn.execute(
        text("SELECT fn_saldo_cliente(CAST(:cliente_id AS BIGINT)) AS saldo"),
        {"cliente_id": p_cliente_id},
    )
    return result.scalar_one()


async def _query_relatorio(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date | None,
    p_data_fim: date | None,
    v_saldo_atual: Decimal | None,
) -> list[RelatorioMensalRow]:
    """Main RETURN QUERY: recursive month CTE joined with aggregated movement."""
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
    result = await conn.execute(
        sql,
        {
            "cliente_id": p_cliente_id,
            "data_inicio": p_data_inicio,
            "data_fim": p_data_fim,
            "saldo_atual": v_saldo_atual if v_saldo_atual is not None else Decimal("0"),
        },
    )
    return [
        RelatorioMensalRow(
            mes_referencia=row["mes_referencia"],
            total_creditos=row["total_creditos"],
            total_debitos=row["total_debitos"],
            saldo_consolidado=row["saldo_consolidado"],
            qtd_transacoes=row["qtd_transacoes"],
        )
        for row in result.mappings()
    ]


async def _query_fallback(
    conn: AsyncConnection,
    p_data_inicio: date | None,
    v_saldo_atual: Decimal | None,
) -> list[RelatorioMensalRow]:
    """Degraded fallback row from the original WHEN OTHERS handler."""
    sql = text(
        """
        SELECT
            DATE_TRUNC('month', CAST(:data_inicio AS DATE))::DATE AS mes_referencia,
            CAST(0 AS NUMERIC(18,2))                              AS total_creditos,
            CAST(0 AS NUMERIC(18,2))                              AS total_debitos,
            COALESCE(CAST(:saldo_atual AS NUMERIC), 0)            AS saldo_consolidado,
            CAST(0 AS INT)                                        AS qtd_transacoes
        """
    )
    result = await conn.execute(
        sql,
        {
            "data_inicio": p_data_inicio,
            "saldo_atual": v_saldo_atual if v_saldo_atual is not None else Decimal("0"),
        },
    )
    return [
        RelatorioMensalRow(
            mes_referencia=row["mes_referencia"],
            total_creditos=row["total_creditos"],
            total_debitos=row["total_debitos"],
            saldo_consolidado=row["saldo_consolidado"],
            qtd_transacoes=row["qtd_transacoes"],
        )
        for row in result.mappings()
    ]


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date | None,
    p_data_fim: date | None,
) -> list[RelatorioMensalRow]:
    """Generate the monthly transaction report for a client.

    Mirrors the original PL/pgSQL block: the period validation, the saldo
    lookup and the report query all sit inside the block protected by the
    WHEN OTHERS handler. Any failure (including the RAISE EXCEPTION for an
    inverted period) is swallowed, logged as a warning and yields exactly
    one degraded fallback row, like the original. The caller owns the
    transaction; no commit/rollback is issued here.
    """
    v_saldo_atual: Decimal | None = None
    try:
        # L27: RAISE EXCEPTION -> typed exception. NULL parameters skip the
        # comparison, exactly as PL/pgSQL does (NULL > NULL is NULL, not true).
        if (
            p_data_inicio is not None
            and p_data_fim is not None
            and p_data_inicio > p_data_fim
        ):
            raise PeriodoInvalidoError(
                f"Periodo invalido: inicio {p_data_inicio} > fim {p_data_fim}"
            )

        # L31: v_saldo_atual := fn_saldo_cliente(p_cliente_id).
        v_saldo_atual = await _fetch_saldo_atual(conn, p_cliente_id)

        # L32: RAISE NOTICE -> logging.
        logger.info("Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual)

        # L34 + L26 EXCEPTION block: the protected statement runs inside a
        # savepoint so the transaction is not left aborted on failure.
        async with conn.begin_nested():
            return await _query_relatorio(
                conn, p_cliente_id, p_data_inicio, p_data_fim, v_saldo_atual
            )
    except Exception as exc:
        # L75: RAISE WARNING -> logging. The savepoint (begin_nested) has
        # already been rolled back by the async with block, so the
        # transaction is usable for the fallback query below.
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )
        return await _query_fallback(conn, p_data_inicio, v_saldo_atual)
