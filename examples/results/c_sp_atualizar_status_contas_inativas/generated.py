"""Modernized version of sp_atualizar_status_contas_inativas.

Marks accounts as 'INATIVA' when they have had no transactions within
``p_dias`` days, then writes an audit log entry. The caller owns the
transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class ProcedureError(Exception):
    """Base typed exception for this module."""


class InvalidParameterError(ProcedureError):
    """Raised when p_dias is null or non-positive."""


@dataclass(frozen=True, slots=True)
class InativacaoResult:
    """Replacement for the OUT parameter p_afetadas."""

    afetadas: int


_DIAS_MIN: Final[int] = 1


async def sp_atualizar_status_contas_inativas(
    conn: AsyncConnection,
    p_dias: int | None,
) -> InativacaoResult:
    """Deactivate accounts without transactions in the last ``p_dias`` days.

    Args:
        conn: Async connection; the caller owns the transaction.
        p_dias: Number of days of inactivity required to deactivate.

    Returns:
        InativacaoResult with the number of affected accounts.

    Raises:
        InvalidParameterError: If ``p_dias`` is None or <= 0.
    """
    if p_dias is None or p_dias <= 0:
        raise InvalidParameterError(
            f"Parametro p_dias deve ser positivo, recebido: {p_dias}"
        )

    cutoff = timedelta(days=p_dias)

    update_result: Result = await conn.execute(
        text(
            """
            UPDATE contas c
               SET status = 'INATIVA'
             WHERE c.status = 'ATIVA'
               AND NOT EXISTS (
                    SELECT 1
                      FROM transacoes t
                     WHERE (t.conta_origem_id = c.id OR t.conta_destino_id = c.id)
                       AND t.data_transacao >= NOW() - CAST(:intervalo AS INTERVAL)
               )
            """
        ),
        {"intervalo": cutoff},
    )
    p_afetadas: int = update_result.rowcount

    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'contas',
                'INATIVACAO_LOTE',
                jsonb_build_object('dias', :dias, 'afetadas', :afetadas)
            )
            """
        ),
        {"dias": p_dias, "afetadas": p_afetadas},
    )

    logger.info(
        "INATIVACAO_LOTE executada: dias=%s afetadas=%s", p_dias, p_afetadas
    )
    return InativacaoResult(afetadas=p_afetadas)
""", "strategy": "hybrid", "architectural_decisions": [{"topic": "Set-based DML kept in SQL", "decision": "The UPDATE with NOT EXISTS and the audit INSERT remain as parameterized SQL via sqlalchemy.text()", "rationale": "Bulk DML and anti-join logic belong close to the database per the guiding principle"}, {"topic": "OUT parameter", "decision": "p_afetadas returned as frozen dataclass InativacaoResult", "rationale": "Rule 6: OUT params map to immutable result types"}, {"topic": "Interval binding", "decision": "Interval computed in Python as timedelta(days=p_dias) and bound via CAST(:intervalo AS INTERVAL) instead of string concatenation (p_dias || ' days')", "rationale": "Avoids SQL string building; parameterized and safe"}, {"topic": "Transaction ownership", "decision": "No commit/rollback in the function", "rationale": "The original procedure runs inside the caller's transaction; caller owns it"}, {"topic": "RAISE EXCEPTION", "decision": "Mapped to typed InvalidParameterError", "rationale": "Rule 5: typed exceptions replace RAISE EXCEPTION"}], "warnings": ["rowcount for UPDATE in SQLAlchemy async reflects rows matched/affected per psycopg/asyncpg semantics; for asyncpg it reports affected rows, matching GET DIAGNOSTICS ROW_COUNT", "The original NOT EXISTS does not filter transacoes by status; cancelled/estornada transactions still count as activity — behaviour preserved as-is", "NOW() is evaluated server-side, so the cutoff uses database time, same as the original"]}