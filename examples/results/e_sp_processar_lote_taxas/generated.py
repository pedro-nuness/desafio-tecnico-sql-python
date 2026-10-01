"""Modernização de sp_processar_lote_taxas (PL/pgSQL -> Python 3.14 + SQLAlchemy 2.x async).

A rotina original percorre, cursor a cursor, as transações efetivadas na data de
referência, calcula a tarifa aplicável por tipo de operação, debita o saldo da
conta de origem, registra a tarifa como nova transação e escreve log de auditoria
em JSONB (por transação e um resumo do lote).

O loop N+1 (4 statements por linha) foi colapsado em UM statement set-based
(CTEs com DML), preservando a semântica de arredondamento NUMERIC(18,2) que o
PL/pgSQL aplicava a cada atribuição de v_taxa.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: object,
) -> None:
    """Processa o lote de tarifas das transações efetivadas em ``p_data_referencia``.

    O chamador é dono da transação: nenhuma commit/rollback é executada aqui
    (a procedure original não controlava transações).
    """
    stmt = text(
        r"""
        WITH base AS (
            SELECT t.id,
                   t.conta_origem_id,
                   t.tipo,
                   t.valor
              FROM transacoes t
             WHERE DATE(t.data_transacao) = CAST(:p_data_referencia AS DATE)
               AND t.status = 'EFETIVADA'
               AND t.tipo <> 'TARIFA'
        ),
        taxa AS (
            SELECT b.id,
                   b.conta_origem_id,
                   b.tipo,
                   b.valor,
                   x.percentual,
                   x.valor_minimo
              FROM base b
              LEFT JOIN LATERAL (
                       SELECT tx.percentual, tx.valor_minimo
                         FROM taxas tx
                        WHERE tx.tipo_operacao = b.tipo
                          AND tx.vigente_de <= CAST(:p_data_referencia AS DATE)
                          AND (tx.vigente_ate IS NULL
                               OR tx.vigente_ate >= CAST(:p_data_referencia AS DATE))
                        ORDER BY tx.vigente_de DESC
                        LIMIT 1
                   ) x ON TRUE
             WHERE x.percentual IS NOT NULL
        ),
        calc AS (
            -- Reproduz o arredondamento NUMERIC(18,2) de cada atribuição de v_taxa:
            -- 1) v_taxa := GREATEST(valor * percentual / 100.0, valor_minimo)
            -- 2) v_taxa := v_taxa * fator (CASE por tipo)
            SELECT id,
                   conta_origem_id,
                   tipo,
                   valor,
                   percentual,
                   CAST(
                       GREATEST(
                           CAST(valor * percentual / 100.0 AS NUMERIC(18,2)),
                           valor_minimo
                       ) AS NUMERIC(18,2)
                   ) AS taxa_base
              FROM taxa
        ),
        calc2 AS (
            SELECT id,
                   conta_origem_id,
                   tipo,
                   valor,
                   percentual,
                   CAST(
                       taxa_base * CASE tipo
                                       WHEN 'TRANSFERENCIA' THEN 1.00
                                       WHEN 'SAQUE'         THEN 1.10
                                       ELSE 0.90
                                   END
                       AS NUMERIC(18,2)
                   ) AS taxa_aplicada
              FROM calc
        ),
        efetivas AS (
            SELECT *
              FROM calc2
             WHERE conta_origem_id IS NOT NULL
        ),
        upd AS (
            -- O UPDATE ... FROM original aplicava uma linha por iteração;
            -- set-based, várias tarifas da mesma conta precisam ser agregadas
            -- antes (regra: UPDATE target FROM source aplica no máximo UMA
            -- linha de source por linha de target).
            UPDATE contas c
               SET saldo = c.saldo - agg.total_taxa
              FROM (
                    SELECT conta_origem_id, SUM(taxa_aplicada) AS total_taxa
                      FROM efetivas
                     GROUP BY conta_origem_id
                   ) agg
             WHERE c.id = agg.conta_origem_id
            RETURNING c.id
        ),
        ins_tarifa AS (
            INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
            SELECT conta_origem_id, 'TARIFA', taxa_aplicada, 'EFETIVADA'
              FROM efetivas
            RETURNING id
        ),
        ins_log_item AS (
            INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
            SELECT 'transacoes',
                   e.id,
                   'TARIFA_APLICADA',
                   jsonb_build_object(
                       'transacao_origem', e.id,
                       'tipo_origem',      e.tipo,
                       'valor_origem',     e.valor,
                       'percentual',       e.percentual,
                       'taxa_aplicada',    e.taxa_aplicada
                   )
              FROM efetivas e
            RETURNING id
        )
        INSERT INTO log_auditoria (entidade, acao, detalhes)
        SELECT 'lote_taxas',
               'LOTE_PROCESSADO',
               jsonb_build_object(
                   'data_referencia', CAST(:p_data_referencia AS DATE),
                   'transacoes',      (SELECT COUNT(*)::INT FROM efetivas),
                   'total_taxas',     (SELECT COALESCE(SUM(taxa_aplicada), 0) FROM efetivas)
               )
        """
    )
    await conn.execute(stmt, {"p_data_referencia": p_data_referencia})
