"""Python 3.14 port of PL/pgSQL function sp_relatorio_mensal_cliente.

Monthly report of credits/debits per month for a client's accounts, with a
recursive CTE generating the month axis. The caller owns the transaction.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger: Final = logging.getLogger(__name__)


class RelatorioMensalError(Exception):
    """Base error for sp_relatorio_mensal_cliente."""


class PeriodoInvalidoError(RelatorioMensalError):
    """RAISE EXCEPTION 'Periodo invalido: inicio % > fim %'."""


@dataclass(frozen=True)
class RelatorioMensalRow:
    """One row of the SETOF table result (columns in declaration order)."""

    mes_referencia: date | None
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


_MAIN_QUERY: Final = text(
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
        m.mes AS mes_referencia,
        CAST(COALESCE(mv.creditos, 0) AS NUMERIC(18, 2)) AS total_creditos,
        CAST(COALESCE(mv.debitos, 0) AS NUMERIC(18, 2)) AS total_debitos,
        CAST(CAST(:v_saldo_atual AS NUMERIC) + COALESCE(mv.creditos, 0)
             - COALESCE(mv.debitos, 0) AS NUMERIC(18, 2)) AS saldo_consolidado,
        CAST(COALESCE(mv.qtd, 0) AS INT) AS qtd_transacoes
      FROM meses m
      LEFT JOIN movimento mv ON mv.mes = m.mes
      ORDER BY m.mes
    """
)

_FALLBACK_QUERY: Final = text(
    """
    SELECT
        DATE_TRUNC('month', CAST(:p_data_inicio AS DATE))::DATE AS mes_referencia,
        CAST(0 AS NUMERIC(18, 2)) AS total_creditos,
        CAST(0 AS NUMERIC(18, 2)) AS total_debitos,
        CAST(COALESCE(CAST(:v_saldo_atual AS NUMERIC), 0) AS NUMERIC(18, 2)) AS saldo_consolidado,
        CAST(0 AS INT) AS qtd_transacoes
    """
)

_SALDO_QUERY: Final = text(
    "SELECT fn_saldo_cliente(CAST(:p_cliente_id AS BIGINT)) AS saldo"
)


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date | None,
    p_data_fim: date | None,
) -> list[RelatorioMensalRow]:
    """Port of sp_relatorio_mensal_cliente (SETOF table -> list of rows).

    The original routine wraps everything in a BEGIN...EXCEPTION WHEN OTHERS
    block that swallows any error, logs a WARNING and returns a single
    fallback row. That control flow is preserved here: the protected
    statements run inside a savepoint (begin_nested) and the handler runs
    after the savepoint is rolled back.

    NULL semantics are preserved: in PL/pgSQL ``p_data_inicio > p_data_fim``
    evaluates to NULL when either date is NULL, so the IF branch (and its
    RAISE EXCEPTION) is skipped and the main query runs with NULL dates,
    yielding a single degraded row carrying the current saldo.
    """
    v_saldo_atual: Decimal | None = None

    # BEGIN (block with exception handler) -> savepoint
    try:
        async with conn.begin_nested():
            # PL/pgSQL: IF NULL-comparison THEN ... -> skipped when either
            # date is NULL (three-valued logic), never raises.
            if (
                p_data_inicio is not None
                and p_data_fim is not None
                and p_data_inicio > p_data_fim
            ):
                raise PeriodoInvalidoError(
                    f"Periodo invalido: inicio {p_data_inicio} > fim {p_data_fim}"
                )

            saldo_row = (
                await conn.execute(_SALDO_QUERY, {"p_cliente_id": p_cliente_id})
            ).one()
            v_saldo_atual = saldo_row.saldo
            if v_saldo_atual is not None and not isinstance(v_saldo_atual, Decimal):
                v_saldo_atual = Decimal(v_saldo_atual)

            logger.info(
                "Saldo atual do cliente %s: %s", p_cliente_id, v_saldo_atual
            )

            result = await conn.execute(
                _MAIN_QUERY,
                {
                    "p_cliente_id": p_cliente_id,
                    "p_data_inicio": p_data_inicio,
                    "p_data_fim": p_data_fim,
                    "v_saldo_atual": v_saldo_atual,
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
                for r in result
            ]
    except Exception as exc:
        # WHEN OTHERS handler: savepoint already rolled back by begin_nested.
        logger.warning(
            "Falha ao gerar relatorio: %s. Retornando linha de fallback.", exc
        )

    # Fallback row (v_saldo_atual stays NULL if the failure happened before
    # the saldo lookup, matching the original COALESCE(v_saldo_atual, 0)).
    fallback = (
        await conn.execute(
            _FALLBACK_QUERY,
            {"p_data_inicio": p_data_inicio, "v_saldo_atual": v_saldo_atual},
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
