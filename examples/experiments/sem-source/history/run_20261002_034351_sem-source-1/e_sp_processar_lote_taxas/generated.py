"""Modernized port of PL/pgSQL procedure sp_processar_lote_taxas.

Applies operation fees to every EFETIVADA, non-TARIFA transaction of a given
reference date: looks up the fee in effect for the operation type, computes
the fee (percentage vs. minimum, adjusted per operation type), debits the
source account, records a TARIFA transaction and a per-item audit entry, then
writes one batch summary audit entry (always, even with zero items).

The original row-by-row cursor loop (4 SQL statements per row) was rewritten
as one set-based data-modifying-CTE statement. The caller owns the
transaction.

Rounding fidelity: the legacy v_taxa variable is NUMERIC(18,2), so it is
rounded to 2 decimal places (half away from zero) on EVERY assignment:
  1) v_taxa = GREATEST(valor * percentual / 100.0, valor_minimo)
  2) v_taxa = v_taxa * (1.0 | 1.10 | 0.90)
Both intermediate roundings are reproduced with CAST(... AS NUMERIC(18,2))
at the exact same points, so per-row taxes, the TARIFA valor, the account
debits and the batch total match the legacy procedure exactly.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: date,
) -> None:
    """Process the fee batch for ``p_data_referencia`` (returns nothing)."""
    stmt = text(
        """
        WITH calc AS (
            SELECT t.id,
                   t.conta_origem_id,
                   t.tipo,
                   t.valor,
                   f.percentual,
                   f.valor_minimo,
                   -- assignment 1: round the GREATEST result to 2 dp
                   CAST(
                       GREATEST(t.valor * f.percentual / 100.0, f.valor_minimo)
                       AS NUMERIC(18,2)
                   ) AS taxa_base,
                   -- assignment 2: apply the operation factor, round again
                   CAST(
                       CAST(
                           GREATEST(t.valor * f.percentual / 100.0, f.valor_minimo)
                           AS NUMERIC(18,2)
                       )
                       * CASE t.tipo
                             WHEN 'TRANSFERENCIA' THEN 1.0
                             WHEN 'SAQUE'         THEN 1.10
                             ELSE 0.90
                         END
                       AS NUMERIC(18,2)
                   ) AS taxa
            FROM transacoes t
            JOIN LATERAL (
                SELECT percentual, valor_minimo
                FROM taxas
                WHERE tipo_operacao = t.tipo
                  AND vigente_de <= CAST(:data_referencia AS DATE)
                  AND (vigente_ate IS NULL
                       OR vigente_ate >= CAST(:data_referencia AS DATE))
                ORDER BY vigente_de DESC
                LIMIT 1
            ) f ON true
            WHERE DATE(t.data_transacao) = CAST(:data_referencia AS DATE)
              AND t.status = 'EFETIVADA'
              AND t.tipo <> 'TARIFA'
              AND t.conta_origem_id IS NOT NULL
        ),
        upd AS (
            -- UPDATE ... FROM applies at most one source row per target row,
            -- so per-account totals are pre-summed. Each taxa is already
            -- NUMERIC(18,2), so the sum is exact and equals the sequential
            -- saldo -= taxa of the original loop.
            UPDATE contas c
               SET saldo = c.saldo - s.total_taxa
            FROM (
                SELECT conta_origem_id, SUM(taxa) AS total_taxa
                FROM calc
                GROUP BY conta_origem_id
            ) s
            WHERE c.id = s.conta_origem_id
        ),
        ins_tarifa AS (
            INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
            SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
            FROM calc
        ),
        ins_log_item AS (
            INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
            SELECT 'transacoes',
                   id,
                   'TARIFA_APLICADA',
                   jsonb_build_object(
                       'transacao_origem', CAST(id AS BIGINT),
                       'tipo_origem',      CAST(tipo AS TEXT),
                       'valor_origem',     CAST(valor AS NUMERIC),
                       'percentual',       CAST(percentual AS NUMERIC),
                       'taxa_aplicada',    CAST(taxa AS NUMERIC)
                   )
            FROM calc
        )
        -- Aggregate without GROUP BY over an empty set still yields one row,
        -- so the batch summary is always written (transacoes = 0,
        -- total_taxas = 0) exactly like the legacy procedure.
        INSERT INTO log_auditoria (entidade, acao, detalhes)
        SELECT 'lote_taxas',
               'LOTE_PROCESSADO',
               jsonb_build_object(
                   'data_referencia', CAST(:data_referencia AS DATE),
                   'transacoes',      CAST(COUNT(*) AS INTEGER),
                   'total_taxas',     CAST(COALESCE(SUM(taxa), 0) AS NUMERIC)
               )
        FROM calc
        """
    )
    await conn.execute(stmt, {"data_referencia": p_data_referencia})
