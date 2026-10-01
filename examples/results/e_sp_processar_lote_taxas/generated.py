"""Python 3.14 port of the PL/pgSQL procedure sp_processar_lote_taxas.

The original cursor loop (4 SQL statements per row) was rewritten as one
set-based CTE statement that reproduces the exact intermediate NUMERIC(18,2)
roundings of the PL/pgSQL variables via CAST(... AS NUMERIC(18,2)).
"""

import logging
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: date,
) -> None:
    """Process fees for all effective transactions of *p_data_referencia*.

    The caller owns the transaction (the original procedure did not manage
    transactions itself).
    """
    # One set-based statement replacing the cursor loop. Each data-modifying
    # CTE runs exactly once; the trailing SELECT returns the aggregate totals
    # needed for the final audit log entry.
    #
    # Rounding fidelity (PL/pgSQL NUMERIC(18,2) rounds on every assignment):
    #   v_taxa := GREATEST(...)            -> CAST(... AS NUMERIC(18,2))
    #   v_taxa := v_taxa * 1.10 / 0.90     -> CAST(... AS NUMERIC(18,2))
    # The UPDATE aggregates taxa per account because the original performed
    # one UPDATE per row (accumulating on saldo).
    result = await conn.execute(
        text(
            """
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
                         AND vigente_de <= CAST(:data_referencia AS DATE)
                         AND (vigente_ate IS NULL
                              OR vigente_ate >= CAST(:data_referencia AS DATE))
                       ORDER BY vigente_de DESC
                       LIMIT 1
                  ) tx ON true
                 WHERE DATE(t.data_transacao) = CAST(:data_referencia AS DATE)
                   AND t.status = 'EFETIVADA'
                   AND t.tipo <> 'TARIFA'
            ),
            calc AS (
                SELECT id,
                       conta_origem_id,
                       tipo,
                       valor,
                       percentual,
                       CAST(
                           CASE tipo
                               WHEN 'TRANSFERENCIA' THEN taxa_base
                               WHEN 'SAQUE'         THEN taxa_base * 1.10
                               ELSE                      taxa_base * 0.90
                           END
                           AS NUMERIC(18,2)
                       ) AS taxa
                  FROM (
                      SELECT id,
                             conta_origem_id,
                             tipo,
                             valor,
                             percentual,
                             CAST(
                                 GREATEST(valor * percentual / 100.0, valor_minimo)
                                 AS NUMERIC(18,2)
                             ) AS taxa_base
                        FROM base
                       WHERE percentual IS NOT NULL
                  ) b
            ),
            applied AS (
                SELECT * FROM calc WHERE conta_origem_id IS NOT NULL
            ),
            upd AS (
                UPDATE contas c
                   SET saldo = c.saldo - s.total_taxa
                  FROM (
                      SELECT conta_origem_id, SUM(taxa) AS total_taxa
                        FROM applied
                       GROUP BY conta_origem_id
                  ) s
                 WHERE c.id = s.conta_origem_id
                RETURNING 1
            ),
            ins_tx AS (
                INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
                SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
                  FROM applied
                RETURNING 1
            ),
            ins_log AS (
                INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
                SELECT 'transacoes',
                       id,
                       'TARIFA_APLICADA',
                       jsonb_build_object(
                           'transacao_origem', id,
                           'tipo_origem',      tipo,
                           'valor_origem',     valor,
                           'percentual',       percentual,
                           'taxa_aplicada',    taxa
                       )
                  FROM applied
                RETURNING 1
            )
            SELECT COUNT(*) AS qtd,
                   COALESCE(SUM(taxa), 0) AS total_taxas
              FROM applied
            """
        ),
        {"data_referencia": p_data_referencia},
    )
    row = result.one()
    v_count: int = int(row.qtd)
    v_total_taxas = row.total_taxas  # Decimal from NUMERIC(18,2) SUM

    logger.info(
        "sp_processar_lote_taxas: data_referencia=%s transacoes=%d total_taxas=%s",
        p_data_referencia,
        v_count,
        v_total_taxas,
    )

    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'lote_taxas', 'LOTE_PROCESSADO',
                jsonb_build_object(
                    'data_referencia', CAST(:data_referencia AS DATE),
                    'transacoes',      CAST(:qtd AS INT),
                    'total_taxas',     CAST(:total_taxas AS NUMERIC(18,2))
                )
            )
            """
        ),
        {
            "data_referencia": p_data_referencia,
            "qtd": v_count,
            "total_taxas": v_total_taxas,
        },
    )
