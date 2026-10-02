"""Modernized version of the PL/pgSQL procedure sp_processar_lote_taxas.

Processes all effective (non-TARIFA) transactions of a reference date, computes
the applicable fee per transaction, debits it from the origin account, records
it as a TARIFA transaction and writes detailed audit logs.

The original row-by-row cursor loop (4 SQL statements per iteration) was
rewritten as set-based statements with identical semantics, including the
NUMERIC(18,2) rounding that PL/pgSQL performed on every assignment of v_taxa.

A NULL reference date is accepted: like the original procedure, it processes
no transactions and only writes the LOTE_PROCESSADO audit row with NULLs.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class FeeProcessingError(RuntimeError):
    """Raised when the fee batch cannot be processed."""


# Set-based rewrite of the cursor loop. Rounding points reproduce the PL/pgSQL
# assignments exactly:
#   v_taxa := GREATEST(v_valor * v_percentual / 100.0, v_minimo)  -> CAST AS NUMERIC(18,2)
#   v_taxa := v_taxa * 1.10 / 1.00 / 0.90                          -> CAST AS NUMERIC(18,2)
# Rows without a vigent fee row (percentual IS NULL) are skipped, exactly like
# the CONTINUE; rows without an origin account are skipped like the
# IF v_origem IS NOT NULL guard.
_CALC_TAXAS_SQL = """
    WITH base AS (
        SELECT t.id,
               t.conta_origem_id,
               t.tipo,
               t.valor,
               tx.percentual,
               tx.valor_minimo
          FROM transacoes t
          LEFT JOIN LATERAL (
              SELECT percentual, valor_minimo
                FROM taxas
               WHERE tipo_operacao = t.tipo
                 AND vigente_de <= CAST(:data_ref AS DATE)
                 AND (vigente_ate IS NULL OR vigente_ate >= CAST(:data_ref AS DATE))
               ORDER BY vigente_de DESC
               LIMIT 1
          ) tx ON true
         WHERE DATE(t.data_transacao) = CAST(:data_ref AS DATE)
           AND t.status = 'EFETIVADA'
           AND t.tipo <> 'TARIFA'
    ),
    calc AS (
        SELECT b.id,
               b.conta_origem_id,
               b.tipo,
               b.valor,
               b.percentual,
               CAST(
                   CAST(GREATEST(b.valor * b.percentual / 100.0, b.valor_minimo)
                        AS NUMERIC(18,2))
                   * CASE b.tipo
                         WHEN 'SAQUE' THEN 1.10
                         WHEN 'TRANSFERENCIA' THEN 1.00
                         ELSE 0.90
                     END
                   AS NUMERIC(18,2)
               ) AS taxa
          FROM base b
         WHERE b.percentual IS NOT NULL
           AND b.conta_origem_id IS NOT NULL
    )
    SELECT id, conta_origem_id, tipo, valor, percentual, taxa
      FROM calc
"""

# One UPDATE per account: the source is aggregated by account first, because
# UPDATE ... FROM applies at most ONE source row per target row and several
# transactions can share the same origin account.
_UPDATE_SALDOS_SQL = text(
    """
    UPDATE contas c
       SET saldo = c.saldo - agg.total_taxas
      FROM (
          SELECT conta_origem_id, SUM(taxa) AS total_taxas
            FROM calc
           GROUP BY conta_origem_id
      ) agg
     WHERE c.id = agg.conta_origem_id
    """
)

_INSERT_TARIFAS_SQL = text(
    """
    INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
    SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
      FROM calc
    """
)

_INSERT_AUDIT_PER_ROW_SQL = text(
    """
    INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
    SELECT 'transacoes',
           id,
           'TARIFA_APLICADA',
           jsonb_build_object(
               'transacao_origem', CAST(id AS BIGINT),
               'tipo_origem',      CAST(tipo AS TEXT),
               'valor_origem',     CAST(valor AS NUMERIC(18,2)),
               'percentual',       CAST(percentual AS NUMERIC(7,4)),
               'taxa_aplicada',    CAST(taxa AS NUMERIC(18,2))
           )
      FROM calc
    """
)

_INSERT_AUDIT_LOTE_SQL = text(
    """
    INSERT INTO log_auditoria (entidade, acao, detalhes)
    VALUES (
        'lote_taxas',
        'LOTE_PROCESSADO',
        jsonb_build_object(
            'data_referencia', CAST(:data_ref AS DATE),
            'transacoes',      CAST(:qtd AS INTEGER),
            'total_taxas',     CAST(:total AS NUMERIC(18,2))
        )
    )
    """
)

_COUNT_SQL = text("SELECT COUNT(*) AS qtd, COALESCE(SUM(taxa), 0) AS total FROM calc")

_DROP_CALC_SQL = text("DROP TABLE IF EXISTS calc")


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: date | None,
) -> None:
    """Process the fee batch for ``p_data_referencia``.

    The caller owns the transaction: this coroutine neither commits nor
    rolls back, mirroring the original procedure which ran inside the
    caller's transaction.

    A ``None`` reference date is preserved from the original behaviour: the
    cursor matches no rows, so only the LOTE_PROCESSADO audit row is written
    (with NULL fields), and nothing is raised.
    """
    # The CTE `calc` is referenced by four subsequent statements; a temporary
    # table materialization keeps all statements consistent within the
    # caller's transaction (the original cursor had the same snapshot
    # semantics).
    await conn.execute(_DROP_CALC_SQL)
    await conn.execute(
        text("CREATE TEMP TABLE calc ON COMMIT DROP AS " + _CALC_TAXAS_SQL),
        {"data_ref": p_data_referencia},
    )

    await conn.execute(_UPDATE_SALDOS_SQL)
    await conn.execute(_INSERT_TARIFAS_SQL)
    await conn.execute(_INSERT_AUDIT_PER_ROW_SQL)

    row = (await conn.execute(_COUNT_SQL)).one()
    qtd: int = int(row.qtd)
    total: Decimal = Decimal(row.total).quantize(Decimal("0.01"))

    logger.info(
        "sp_processar_lote_taxas: data_referencia=%s transacoes=%d total_taxas=%s",
        p_data_referencia,
        qtd,
        total,
    )

    await conn.execute(
        _INSERT_AUDIT_LOTE_SQL,
        {"data_ref": p_data_referencia, "qtd": qtd, "total": total},
    )

    await conn.execute(_DROP_CALC_SQL)
