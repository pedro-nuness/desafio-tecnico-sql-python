from __future__ import annotations

import logging
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# Shared CTE: reproduces the cursor query plus the per-row fee lookup and
# fee computation. The lateral join drops rows without a vigente fee rule
# (the original CONTINUE) and the WHERE drops NULL conta_origem_id rows
# (the original IF v_origem IS NOT NULL).
# v_taxa is declared plain NUMERIC in the original: no intermediate
# rounding; the target columns (NUMERIC(18,2)) round on write.
# CASE: TRANSFERENCIA keeps the fee, SAQUE multiplies by 1.10, others 0.90.
_CTE_SQL: Final[str] = """
WITH transacoes_dia AS (
    SELECT t.id, t.conta_origem_id, t.tipo, t.valor
      FROM transacoes t
     WHERE DATE(t.data_transacao) = CAST(:data_referencia AS DATE)
       AND t.status = 'EFETIVADA'
       AND t.tipo <> 'TARIFA'
),
com_taxa AS (
    SELECT td.id,
           td.conta_origem_id,
           td.tipo,
           td.valor,
           tx.percentual,
           GREATEST(td.valor * tx.percentual / 100.0, tx.valor_minimo)
               * CASE td.tipo
                     WHEN 'TRANSFERENCIA' THEN 1.00
                     WHEN 'SAQUE'         THEN 1.10
                     ELSE 0.90
                 END AS taxa
      FROM transacoes_dia td
      CROSS JOIN LATERAL (
          SELECT x.percentual, x.valor_minimo
            FROM taxas x
           WHERE x.tipo_operacao = td.tipo
             AND x.vigente_de <= CAST(:data_referencia AS DATE)
             AND (x.vigente_ate IS NULL
                  OR x.vigente_ate >= CAST(:data_referencia AS DATE))
           ORDER BY x.vigente_de DESC
           LIMIT 1
      ) tx
     WHERE td.conta_origem_id IS NOT NULL
)
"""

_SQL_DEBITO: Final[str] = _CTE_SQL + """
, taxas_por_conta AS (
    SELECT conta_origem_id, SUM(taxa) AS total_taxa
      FROM com_taxa
     GROUP BY conta_origem_id
)
UPDATE contas c
   SET saldo = c.saldo - tpc.total_taxa
  FROM taxas_por_conta tpc
 WHERE c.id = tpc.conta_origem_id
"""

_SQL_INSERT_TARIFAS: Final[str] = _CTE_SQL + """
INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
  FROM com_taxa
"""

_SQL_INSERT_AUDITORIA: Final[str] = _CTE_SQL + """
INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
SELECT
    'transacoes',
    id,
    'TARIFA_APLICADA',
    jsonb_build_object(
        'transacao_origem', CAST(id AS BIGINT),
        'tipo_origem',      CAST(tipo AS TEXT),
        'valor_origem',     CAST(valor AS NUMERIC),
        'percentual',       CAST(percentual AS NUMERIC),
        'taxa_aplicada',    CAST(taxa AS NUMERIC)
    )
  FROM com_taxa
"""

_SQL_SUMARIO: Final[str] = _CTE_SQL + """
SELECT COUNT(*) AS v_count,
       COALESCE(SUM(taxa), 0) AS v_total_taxas
  FROM com_taxa
"""

_SQL_LOG_LOTE: Final[str] = """
INSERT INTO log_auditoria (entidade, acao, detalhes)
VALUES (
    'lote_taxas',
    'LOTE_PROCESSADO',
    jsonb_build_object(
        'data_referencia', CAST(CAST(:data_referencia AS DATE) AS TEXT),
        'transacoes',      CAST(:v_count AS INTEGER),
        'total_taxas',     CAST(:v_total_taxas AS NUMERIC)
    )
)
"""


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: object,
) -> None:
    """Python port of procedure sp_processar_lote_taxas(date).

    The original routine iterated a cursor over the day's effective
    (non-TARIFA) transactions and, per row: looked up the applicable fee
    rule, computed the fee, debited the account, inserted a TARIFA
    transaction and an audit log row; finally it logged the batch summary.

    Faithfulness notes:
      - v_taxa is declared plain NUMERIC (no precision): no intermediate
        rounding in PL/pgSQL; the NUMERIC(18,2) target columns round on
        write, exactly as here.
      - The CASE keeps the fee unchanged for TRANSFERENCIA, multiplies by
        1.10 for SAQUE and by 0.90 for any other tipo.
      - v_total_taxas is initialized to 0 and only accumulates inside the
        per-row IF, so an empty batch (including a NULL reference date)
        still writes the summary log with total_taxas 0 and transacoes 0.
      - Rows without a vigente fee rule, and rows with NULL
        conta_origem_id, are skipped exactly as the original
        CONTINUE / IF did.

    The per-row loop (N+1: 4 statements per iteration) is collapsed into
    set-based statements with identical semantics; the account debit
    aggregates the fees per conta_origem_id because UPDATE ... FROM
    applies at most one source row per target row.

    The caller owns the transaction: no commit/rollback is issued here.
    """
    params = {"data_referencia": p_data_referencia}

    # Summary counters first (read-only over the day's transactions;
    # the inserted TARIFA rows never match the cursor predicate).
    summary = await conn.execute(text(_SQL_SUMARIO), params)
    row = summary.one()
    v_count: int = int(row.v_count)
    v_total_taxas: Decimal = row.v_total_taxas

    # Debit: aggregate per account (UPDATE ... FROM applies one source row).
    await conn.execute(text(_SQL_DEBITO), params)

    # TARIFA transactions, one per processed transaction.
    await conn.execute(text(_SQL_INSERT_TARIFAS), params)

    # Per-transaction audit rows.
    await conn.execute(text(_SQL_INSERT_AUDITORIA), params)

    # Final batch summary log (written even when nothing was processed).
    await conn.execute(
        text(_SQL_LOG_LOTE),
        {
            "data_referencia": p_data_referencia,
            "v_count": v_count,
            "v_total_taxas": v_total_taxas,
        },
    )

    logger.info(
        "sp_processar_lote_taxas: data_referencia=%s transacoes=%d total_taxas=%s",
        p_data_referencia,
        v_count,
        v_total_taxas,
    )
