from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class LoteTaxasError(Exception):
    """Erro ao processar o lote de taxas."""


@dataclass(frozen=True)
class LoteTaxasResumo:
    """Resumo do lote processado (tambem gravado em log_auditoria)."""

    data_referencia: date
    transacoes: int
    total_taxas: Decimal


# Whitelist de identificadores dinamicos (nenhum identificador e montado
# dinamicamente nesta rotina; a whitelist existe para conformidade futura).
_TABELAS_PERMITIDAS: Final[frozenset[str]] = frozenset(
    {"transacoes", "contas", "taxas", "log_auditoria"}
)

# Um unico statement set-based substitui o cursor row-a-row do PL/pgSQL.
# CTEs de dados modificam o banco na mesma ordem logica do original:
#   base   -> transacoes elegiveis com a taxa calculada (lateral na tabela taxas)
#   upd    -> debito do saldo nas contas de origem
#   ins_tx -> novas transacoes do tipo TARIFA
#   ins_log-> auditoria por transacao (JSONB)
#   tot    -> agregado do lote
#   insert final -> log consolidado LOTE_PROCESSADO
_SQL_PROCESSAR_LOTE: Final[str] = """
WITH base AS (
    SELECT
        t.id                AS id,
        t.conta_origem_id   AS conta_origem_id,
        t.tipo              AS tipo,
        t.valor             AS valor,
        x.percentual        AS percentual,
        x.valor_minimo      AS valor_minimo,
        GREATEST(
            t.valor * x.percentual / 100.0,
            x.valor_minimo
        ) * CASE t.tipo
                WHEN 'TRANSFERENCIA' THEN 1.00
                WHEN 'SAQUE'         THEN 1.10
                ELSE                      0.90
            END AS taxa
    FROM transacoes t
    LEFT JOIN LATERAL (
        SELECT percentual, valor_minimo
          FROM taxas
         WHERE tipo_operacao = t.tipo
           AND vigente_de <= :p_data_referencia
           AND (vigente_ate IS NULL OR vigente_ate >= :p_data_referencia)
         ORDER BY vigente_de DESC
         LIMIT 1
    ) x ON TRUE
    WHERE DATE(t.data_transacao) = :p_data_referencia
      AND t.status = 'EFETIVADA'
      AND t.tipo <> 'TARIFA'
      AND t.conta_origem_id IS NOT NULL
      AND x.percentual IS NOT NULL
),
upd AS (
    UPDATE contas c
       SET saldo = c.saldo - b.taxa
      FROM base b
     WHERE c.id = b.conta_origem_id
),
ins_tx AS (
    INSERT INTO transacoes (conta_origem_id, tipo, valor, status)
    SELECT conta_origem_id, 'TARIFA', taxa, 'EFETIVADA'
      FROM base
),
ins_log AS (
    INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
    SELECT
        'transacoes',
        id,
        'TARIFA_APLICADA',
        jsonb_build_object(
            'transacao_origem', id,
            'tipo_origem',      tipo,
            'valor_origem',     valor,
            'percentual',       percentual,
            'taxa_aplicada',    taxa
        )
      FROM base
),
tot AS (
    SELECT COUNT(*)::int AS cnt, COALESCE(SUM(taxa), 0)::numeric(18, 2) AS total
      FROM base
)
INSERT INTO log_auditoria (entidade, acao, detalhes)
SELECT
    'lote_taxas',
    'LOTE_PROCESSADO',
    jsonb_build_object(
        'data_referencia', :p_data_referencia,
        'transacoes',      cnt,
        'total_taxas',     total
    )
FROM tot
RETURNING
    (detalhes ->> 'transacoes')::int  AS transacoes,
    (detalhes ->> 'total_taxas')::numeric AS total_taxas
"""


def _validar_data(p_data_referencia: date) -> None:
    if not isinstance(p_data_referencia, date):
        raise LoteTaxasError("p_data_referencia deve ser um date")


def _row_para_resumo(row: RowMapping, data_referencia: date) -> LoteTaxasResumo:
    return LoteTaxasResumo(
        data_referencia=data_referencia,
        transacoes=int(row["transacoes"]),
        total_taxas=Decimal(str(row["total_taxas"])),
    )


async def sp_processar_lote_taxas(
    conn: AsyncConnection,
    p_data_referencia: date,
) -> LoteTaxasResumo:
    """Processa o lote de taxas das transacoes efetivadas em ``p_data_referencia``.

    Substitui o cursor row-a-row da procedure PL/pgSQL original por um unico
    statement set-based (CTEs de dados), preservando:
      - criterio de elegibilidade (data, status EFETIVADA, tipo <> TARIFA);
      - busca da taxa vigente mais recente por tipo de operacao;
      - formula GREATEST(valor * percentual / 100, minimo) com multiplicador
        por tipo (TRANSFERENCIA 1.00, SAQUE 1.10, demais 0.90);
      - debito do saldo, insercao da transacao TARIFA e logs de auditoria;
      - log consolidado LOTE_PROCESSADO.

    A transacao e de responsabilidade do chamador (a procedure original nao
    gerenciava commit/rollback explicitamente).
    """
    _validar_data(p_data_referencia)

    logger.info(
        "Processando lote de taxas para data_referencia=%s",
        p_data_referencia.isoformat(),
    )

    result = await conn.execute(
        text(_SQL_PROCESSAR_LOTE),
        {"p_data_referencia": p_data_referencia},
    )

    rows = result.mappings().all()
    if not rows:
        # Nenhuma transacao elegivel: o INSERT final do log nao retornou linha.
        # Comportamento equivalente ao original (v_count = 0, v_total_taxas = 0),
        # porem o log LOTE_PROCESSADO e gravado pelo proprio statement quando
        # existe ao menos o agregado; sem linhas elegiveis o CTE tot ainda
        # produz uma linha (COUNT = 0), entao este ramo e defensivo.
        logger.warning(
            "Lote de taxas sem linha de retorno para data_referencia=%s",
            p_data_referencia.isoformat(),
        )
        return LoteTaxasResumo(
            data_referencia=p_data_referencia,
            transacoes=0,
            total_taxas=Decimal("0"),
        )

    resumo = _row_para_resumo(rows[0], p_data_referencia)
    logger.info(
        "Lote processado: transacoes=%d total_taxas=%s",
        resumo.transacoes,
        resumo.total_taxas,
    )
    return resumo
