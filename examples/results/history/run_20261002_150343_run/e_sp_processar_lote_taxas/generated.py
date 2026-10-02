import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: datetime.date,
) -> None:
    """Processa as tarifas das transacoes efetivadas em ``p_data_referencia``.

    Equivalente set-based da procedure PL/pgSQL original: em vez de um cursor
    com 4 statements por linha, uma unica instrucao com CTEs calcula a taxa de
    cada transacao (reproduzindo os arredondamentos intermediarios de
    NUMERIC(18,2) da original), debita os saldos agregados por conta, insere as
    transacoes TARIFA e os logs de auditoria por transacao. O log final do
    lote e inserido em um segundo statement, usando o total e a contagem
    retornados pela primeira.

    O caller possui a transacao: nenhuma confirmacao/rollback e feita aqui.
    """
    resumo = await conn.execute(
        text(
            """
            WITH transacoes_dia AS (
                SELECT t.id, t.conta_origem_id, t.tipo, t.valor
                  FROM transacoes t
                 WHERE DATE(t.data_transacao) = CAST(:data_ref AS DATE)
                   AND t.status = 'EFETIVADA'
                   AND t.tipo <> 'TARIFA'
            ),
            taxa_vigente AS (
                SELECT t.id, t.conta_origem_id, t.tipo, t.valor,
                       x.percentual, x.valor_minimo
                  FROM transacoes_dia t
                  LEFT JOIN LATERAL (
                      SELECT percentual, valor_minimo
                        FROM taxas
                       WHERE tipo_operacao = t.tipo
                         AND vigente_de <= CAST(:data_ref AS DATE)
                         AND (vigente_ate IS NULL
                              OR vigente_ate >= CAST(:data_ref AS DATE))
                       ORDER BY vigente_de DESC
                       LIMIT 1
                  ) x ON true
                 WHERE t.conta_origem_id IS NOT NULL
                   AND x.percentual IS NOT NULL
            ),
            fee_base AS (
                -- v_taxa := GREATEST(v_valor * v_percentual / 100.0, v_minimo)
                -- atribuida a NUMERIC(18,2): arredonda aqui.
                SELECT id, conta_origem_id, tipo, valor, percentual,
                       CAST(GREATEST(valor * percentual / 100.0, valor_minimo)
                            AS NUMERIC(18,2)) AS taxa_base
                  FROM taxa_vigente
            ),
            fees AS (
                -- CASE v_tipo ... atribuido novamente a NUMERIC(18,2):
                -- segundo arredondamento.
                SELECT id, conta_origem_id, tipo, valor, percentual,
                       CASE tipo
                           WHEN 'TRANSFERENCIA' THEN taxa_base
                           WHEN 'SAQUE' THEN CAST(taxa_base * 1.10 AS NUMERIC(18,2))
                           ELSE CAST(taxa_base * 0.90 AS NUMERIC(18,2))
                       END AS taxa
                  FROM fee_base
            ),
            upd_saldo AS (
                -- UPDATE ... FROM aplica no maximo 1 linha de origem por
                -- alvo: agrega as taxas por conta antes de debitar.
                UPDATE contas c
                   SET saldo = c.saldo - agg.total_taxas
                  FROM (
                      SELECT conta_origem_id,
                             CAST(SUM(taxa) AS NUMERIC(18,2)) AS total_taxas
                        FROM fees
                       GROUP BY conta_origem_id
                  ) agg
                 WHERE c.id = agg.conta_origem_id
            ),
            ins_tarifa AS (
                INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
                SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
                  FROM fees
                RETURNING id
            ),
            ins_log AS (
                -- O log referencia a transacao de ORIGEM (v_id), nao a TARIFA
                -- recem-inserida; portanto nao depende de ins_tarifa.
                INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
                SELECT 'transacoes', f.id, 'TARIFA_APLICADA',
                       jsonb_build_object(
                           'transacao_origem', f.id,
                           'tipo_origem',      f.tipo,
                           'valor_origem',     f.valor,
                           'percentual',       f.percentual,
                           'taxa_aplicada',    f.taxa
                       )
                  FROM fees f
            )
            SELECT CAST(COUNT(*) AS INTEGER) AS qtd,
                   CAST(COALESCE(SUM(taxa), 0) AS NUMERIC(18,2)) AS total
              FROM fees
            """
        ),
        {"data_ref": p_data_referencia},
    )
    row = resumo.one()
    v_count: int = row.qtd
    v_total_taxas: datetime.date = row.total  # type: ignore[assignment]

    await conn.execute(
        text(
            """
            INSERT INTO log_auditoria (entidade, acao, detalhes)
            VALUES (
                'lote_taxas', 'LOTE_PROCESSADO',
                jsonb_build_object(
                    'data_referencia', CAST(:data_ref AS DATE),
                    'transacoes',      CAST(:qtd AS INTEGER),
                    'total_taxas',     CAST(:total AS NUMERIC(18,2))
                )
            )
            """
        ),
        {
            "data_ref": p_data_referencia,
            "qtd": v_count,
            "total": v_total_taxas,
        },
    )
