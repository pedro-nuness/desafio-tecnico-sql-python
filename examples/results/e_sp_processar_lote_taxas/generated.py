from __future__ import annotations

import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class FeeBatchError(Exception):
    """Raised when the fee batch processing cannot proceed."""


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: datetime.date,
) -> None:
    """Python port of procedure sp_processar_lote_taxas(DATE).

    The original routine looped over each transaction of the reference date,
    looked up the applicable fee, adjusted the account balance, inserted a
    TARIFA transaction and an audit log row, one SQL statement per row
    (N+1). Here the whole loop is collapsed into a single set-based
    statement: a CTE computes the fee per transaction (reproducing the
    intermediate NUMERIC(18,2) roundings of the PL/pgSQL assignments via
    CAST(... AS NUMERIC(18,2))), and data-modifying CTEs perform the balance
    update (with the source aggregated per account, since UPDATE ... FROM
    applies at most one source row per target row), the TARIFA inserts and
    the per-transaction audit rows. The final batch audit row is written in
    the same statement, reading the same CTE snapshot.

    Transaction control is owned by the caller (the original procedure does
    not commit), so no commit/rollback is issued here.
    """
    await conn.execute(
        text(
            """
            WITH transacoes_dia AS (
                SELECT t.id, t.conta_origem_id, t.tipo, t.valor
                  FROM transacoes t
                 WHERE DATE(t.data_transacao) = CAST(:p_data_referencia AS DATE)
                   AND t.status = 'EFETIVADA'
                   AND t.tipo <> 'TARIFA'
            ),
            fees AS (
                SELECT td.id,
                       td.conta_origem_id,
                       td.tipo AS tipo_origem,
                       td.valor AS valor_origem,
                       tx.percentual,
                       CAST(
                           GREATEST(
                               CAST(td.valor * tx.percentual / 100.0 AS NUMERIC(18,2)),
                               tx.valor_minimo
                           )
                           * CASE td.tipo
                                 WHEN 'SAQUE' THEN 1.10
                                 WHEN 'TRANSFERENCIA' THEN 1.0
                                 ELSE 0.90
                             END
                           AS NUMERIC(18,2)
                       ) AS taxa
                  FROM transacoes_dia td
                  LEFT JOIN LATERAL (
                      SELECT percentual, valor_minimo
                        FROM taxas
                       WHERE tipo_operacao = td.tipo
                         AND vigente_de <= CAST(:p_data_referencia AS DATE)
                         AND (vigente_ate IS NULL OR vigente_ate >= CAST(:p_data_referencia AS DATE))
                       ORDER BY vigente_de DESC
                       LIMIT 1
                  ) tx ON TRUE
                 WHERE tx.percentual IS NOT NULL
                   AND td.conta_origem_id IS NOT NULL
            ),
            upd AS (
                UPDATE contas c
                   SET saldo = c.saldo - f.total
                  FROM (
                      SELECT conta_origem_id, SUM(taxa) AS total
                        FROM fees
                       GROUP BY conta_origem_id
                  ) f
                 WHERE c.id = f.conta_origem_id
            ),
            ins_tx AS (
                INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
                SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
                  FROM fees
            ),
            ins_log AS (
                INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
                SELECT 'transacoes', id, 'TARIFA_APLICADA',
                       jsonb_build_object(
                           'transacao_origem', id,
                           'tipo_origem',      tipo_origem,
                           'valor_origem',     valor_origem,
                           'percentual',       percentual,
                           'taxa_aplicada',    taxa
                       )
                  FROM fees
            )
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            SELECT 'lote_taxas', 'LOTE_PROCESSADO',
                   jsonb_build_object(
                       'data_referencia', CAST(:p_data_referencia AS DATE),
                       'transacoes',      (SELECT COUNT(*) FROM fees),
                       'total_taxas',     (SELECT COALESCE(SUM(taxa), 0) FROM fees)
                   )
            """
        ),
        {"p_data_referencia": p_data_referencia},
    )
    logger.info(
        "sp_processar_lote_taxas executado para data_referencia=%s",
        p_data_referencia,
    )
