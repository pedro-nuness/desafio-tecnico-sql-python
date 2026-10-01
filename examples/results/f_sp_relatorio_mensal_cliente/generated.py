"""Modernized port of sp_relatorio_mensal_cliente (PL/pgSQL -> Python 3.14).

Generates a monthly movement report for a client. The recursive CTE, the
aggregation and the joins stay in SQL; Python only validates inputs, fetches
the current balance (fn_saldo_cliente), coordinates the degraded fallback and
shapes the typed result rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(ValueError):
    """Raised when p_data_inicio > p_data_fim (original RAISE EXCEPTION)."""


@dataclass(frozen=True)
class RelatorioMensalRow:
    """One row of the SETOF returned by the original routine."""

    mes_referencia: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


def _quantize_2(value: Decimal) -> Decimal:
    """Reproduce NUMERIC(18,2) assignment rounding (half away from zero)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date,
    p_data_fim: date,
) -> list[RelatorioMensalRow]:
    """Port of sp_relatorio_mensal_cliente.

    The caller owns the transaction; the original EXCEPTION ... WHEN OTHERS
    block is reproduced with a savepoint (begin_nested) so a failure inside
    the protected statements does not abort the surrounding transaction.
    """
    v_saldo_atual: Decimal | None = None

    # L26 BLOCK protected by EXCEPTION WHEN OTHERS -> savepoint.
    try:
        async with conn.begin_nested():
            # L27-L29: validation RAISE inside the protected block (the original
            # WHEN OTHERS handler also catches it and returns the fallback row).
            if p_data_inicio > p_data_fim:
                raise PeriodoInvalidoError(
                    f"Periodo invalido: inicio {p_data_inicio} > fim {p_data_fim}"
                )

            # L31: v_saldo_atual := fn_saldo_cliente(p_cliente_id)
            # NUMERIC(18,2) assignment rounds to 2 decimal places.
            row = (
                await conn.execute(
                    text("SELECT fn_saldo_cliente(CAST(:cliente_id AS BIGINT)) AS saldo"),
                    {"cliente_id": p_cliente_id},
                )
            ).one()
            v_saldo_atual = _quantize_2(Decimal(str(row.saldo)))

            # L32: RAISE NOTICE
            logger.info(
                "Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual
            )

            # L34: RETURN QUERY - recursive CTE + aggregation stay in SQL.
            result = await conn.execute(
                text(
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
                ),
                {
                    "cliente_id": p_cliente_id,
                    "data_inicio": p_data_inicio,
                    "data_fim": p_data_fim,
                    "saldo_atual": v_saldo_atual,
                },
            )
            return [
                RelatorioMensalRow(
                    mes_referencia=r.mes_referencia,
                    total_creditos=r.total_creditos,
                    total_debitos=r.total_debitos,
                    saldo_consolidado=r.saldo_consolidado,
                    qtd_transacoes=r.qtd_transacoes,
                )
                for r in result.mappings()
            ]
    except Exception as exc:  # original WHEN OTHERS: swallow and degrade
        # L75: RAISE WARNING
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )

        # L77: fallback RETURN QUERY (v_saldo_atual may still be None -> COALESCE 0).
        fallback = await conn.execute(
            text(
                """
                SELECT
                    DATE_TRUNC('month', CAST(:data_inicio AS DATE))::DATE AS mes_referencia,
                    CAST(0 AS NUMERIC(18, 2))  AS total_creditos,
                    CAST(0 AS NUMERIC(18, 2))  AS total_debitos,
                    CAST(COALESCE(CAST(:saldo_atual AS NUMERIC), 0) AS NUMERIC(18, 2))
                                               AS saldo_consolidado,
                    CAST(0 AS INT)             AS qtd_transacoes
                """
            ),
            {"data_inicio": p_data_inicio, "saldo_atual": v_saldo_atual},
        )
        r = fallback.mappings().one()
        return [
            RelatorioMensalRow(
                mes_referencia=r.mes_referencia,
                total_creditos=r.total_creditos,
                total_debitos=r.total_debitos,
                saldo_consolidado=r.saldo_consolidado,
                qtd_transacoes=r.qtd_transacoes,
            )
        ]
