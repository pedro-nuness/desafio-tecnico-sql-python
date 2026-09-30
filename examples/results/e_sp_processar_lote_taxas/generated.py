"""Modernized port of PL/pgSQL procedure sp_processar_lote_taxas.

Original routine looped over the day's effective transactions, looked up the
applicable fee row per transaction, applied a type-specific multiplier,
debited the account, inserted a TARIFA transaction and wrote audit logs.

The row-by-row loop (N+1 over four statements) is collapsed into a single
set-based statement using data-modifying CTEs. The caller owns the
transaction: this function neither commits nor rolls back.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncConnection

logger: Final[logging.Logger] = logging.getLogger(__name__)

# Set-based rewrite of the original cursor loop. All per-row work (fee
# lookup, fee calculation, account debit, TARIFA insert, audit log) happens
# inside one statement. The account debit aggregates fees per account so
# that accounts touched by several transactions are debited exactly once
# with the summed amount, matching the cumulative effect of the original
# sequential UPDATEs.
_PROCESSAMENTO_SQL: Final[str] = """
WITH transacoes_dia AS (
    SELECT id, conta_origem_id, tipo, valor
      FROM transacoes
     WHERE DATE(data_transacao) = :data_referencia
       AND status = 'EFETIVADA'
       AND tipo <> 'TARIFA'
),
com_taxa AS (
    SELECT t.id,
           t.conta_origem_id,
           t.tipo,
           t.valor,
           x.percentual,
           x.valor_minimo
      FROM transacoes_dia t
      LEFT JOIN LATERAL (
           SELECT percentual, valor_minimo
             FROM taxas
            WHERE tipo_operacao = t.tipo
              AND vigente_de <= :data_referencia
              AND (vigente_ate IS NULL OR vigente_ate >= :data_referencia)
            ORDER BY vigente_de DESC
            LIMIT 1
      ) x ON true
),
calculadas AS (
    SELECT c.*,
           GREATEST(c.valor * c.percentual / 100.0, c.valor_minimo)
               * CASE c.tipo
                     WHEN 'TRANSFERENCIA' THEN 1.0
                     WHEN 'SAQUE'         THEN 1.10
                     ELSE 0.90
                 END AS taxa
      FROM com_taxa c
     WHERE c.percentual IS NOT NULL
       AND c.conta_origem_id IS NOT NULL
),
debitos AS (
    SELECT conta_origem_id, SUM(taxa) AS taxa_total
      FROM calculadas
     GROUP BY conta_origem_id
),
atualizadas AS (
    UPDATE contas
       SET saldo = saldo - d.taxa_total
      FROM debitos d
     WHERE contas.id = d.conta_origem_id
    RETURNING contas.id
),
tarifas_inseridas AS (
    INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
    SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
      FROM calculadas
    RETURNING id
),
log_por_transacao AS (
    INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
    SELECT 'transacoes',
           c.id,
           'TARIFA_APLICADA',
           jsonb_build_object(
               'transacao_origem', c.id,
               'tipo_origem',      c.tipo,
               'valor_origem',     c.valor,
               'percentual',       c.percentual,
               'taxa_aplicada',    c.taxa
           )
      FROM calculadas c
),
log_lote AS (
    INSERT INTO log_auditoria (entidade, acao, detalhes)
    SELECT 'lote_taxas',
           'LOTE_PROCESSADO',
           jsonb_build_object(
               'data_referencia', :data_referencia,
               'transacoes',      COUNT(*),
               'total_taxas',     COALESCE(SUM(taxa), 0)
           )
      FROM calculadas
)
SELECT
    (SELECT COUNT(*) FROM tarifas_inseridas) AS transacoes_processadas,
    (SELECT COALESCE(SUM(taxa), 0) FROM calculadas) AS total_taxas,
    (SELECT COUNT(*) FROM atualizadas) AS contas_debitadas
"""


class LoteTaxasError(Exception):
    """Base error for the fee-batch processing routine."""


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: date,
) -> None:
    """Process all fees for the transactions effective on ``p_data_referencia``.

    Port of the PL/pgSQL procedure ``sp_processar_lote_taxas``. The original
    cursor loop was rewritten as one set-based statement (see module
    docstring). The caller owns the transaction: no commit/rollback is
    issued here.

    :param conn: active async connection/transaction owned by the caller.
    :param p_data_referencia: reference date whose transactions are charged.
    :raises LoteTaxasError: if the batch statement fails to execute.
    """
    try:
        result: Result = await conn.execute(
            text(_PROCESSAMENTO_SQL),
            {"data_referencia": p_data_referencia},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as typed error
        logger.error(
            "Falha ao processar lote de taxas para data_referencia=%s: %s",
            p_data_referencia,
            exc,
        )
        raise LoteTaxasError(
            f"Falha ao processar lote de taxas para {p_data_referencia!s}"
        ) from exc

    row = result.one()
    logger.info(
        "Lote de taxas processado (data_referencia=%s): "
        "transacoes=%d, total_taxas=%s, contas_debitadas=%d",
        p_data_referencia,
        row.transacoes_processadas,
        row.total_taxas,
        row.contas_debitadas,
    )
