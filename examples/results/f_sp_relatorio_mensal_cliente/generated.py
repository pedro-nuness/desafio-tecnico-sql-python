'''sp_relatorio_mensal_cliente: relatorio mensal de movimentacao por cliente.

Migrado de PL/pgSQL para Python 3.14 + SQLAlchemy 2.x async.
A logica relacional (CTE recursiva de meses, agregacao de transacoes)
permanece em SQL parametrizado; Python coordena validacao, chamada a
fn_saldo_cliente, tratamento de erros e montagem do resultado.
'''

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class PeriodoInvalidoError(Exception):
    '''RAISE EXCEPTION original: periodo invalido (inicio > fim).'''

    def __init__(self, inicio: date, fim: date) -> None:
        super().__init__(f'Periodo invalido: inicio {inicio} > fim {fim}')
        self.inicio = inicio
        self.fim = fim


@dataclass(frozen=True)
class RelatorioMensalRow:
    '''Linha retornada (SETOF TABLE), colunas na ordem original.'''

    mes_referencia: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int


def _q2(value: Decimal) -> Decimal:
    '''Reproduz o arredondamento de NUMERIC(18,2) a cada atribuicao
    (half away from zero; para valores positivos equivale a ROUND_HALF_UP).'''
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


_RELATORIO_SQL = text(r'''
WITH RECURSIVE meses AS (
    SELECT DATE_TRUNC('month', CAST(:p_data_inicio AS DATE))::DATE AS mes
    UNION ALL
    SELECT (mes + INTERVAL '1 month')::DATE
      FROM meses
     WHERE mes < DATE_TRUNC('month', CAST(:p_data_fim AS DATE))
),
movimento AS (
    SELECT
        DATE_TRUNC('month', t.data_transacao)::DATE AS mes,
        SUM(CASE WHEN t.conta_destino_id IN (
                SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
            ) THEN t.valor ELSE 0 END) AS creditos,
        SUM(CASE WHEN t.conta_origem_id IN (
                SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT)
            ) THEN t.valor ELSE 0 END) AS debitos,
        COUNT(*) AS qtd
      FROM transacoes t
     WHERE t.status = 'EFETIVADA'
       AND t.data_transacao >= CAST(:p_data_inicio AS DATE)
       AND t.data_transacao <  CAST(:p_data_fim AS DATE) + INTERVAL '1 day'
       AND (
           t.conta_origem_id  IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT))
        OR t.conta_destino_id IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS BIGINT))
       )
     GROUP BY 1
)
SELECT
    m.mes                                                    AS mes_referencia,
    COALESCE(mv.creditos, 0)                                 AS total_creditos,
    COALESCE(mv.debitos, 0)                                  AS total_debitos,
    CAST(:v_saldo_atual AS NUMERIC) + COALESCE(mv.creditos, 0)
                                  - COALESCE(mv.debitos, 0)  AS saldo_consolidado,
    COALESCE(mv.qtd, 0)::INT                                 AS qtd_transacoes
  FROM meses m
  LEFT JOIN movimento mv ON mv.mes = m.mes
 ORDER BY m.mes
''')

_FALLBACK_SQL = text(r'''
SELECT
    DATE_TRUNC('month', CAST(:p_data_inicio AS DATE))::DATE AS mes_referencia,
    CAST(0 AS NUMERIC(18,2))                                 AS total_creditos,
    CAST(0 AS NUMERIC(18,2))                                 AS total_debitos,
    COALESCE(CAST(:v_saldo_atual AS NUMERIC), 0)             AS saldo_consolidado,
    CAST(0 AS INT)                                           AS qtd_transacoes
''')

_SALDO_SQL = text('SELECT fn_saldo_cliente(CAST(:p_cliente_id AS BIGINT)) AS saldo')


async def sp_relatorio_mensal_cliente(
    conn: AsyncConnection,
    p_cliente_id: int,
    p_data_inicio: date,
    p_data_fim: date,
) -> list[RelatorioMensalRow]:
    '''Gera o relatorio mensal de movimentacao do cliente.

    O caller possui a transacao. O bloco PL/pgSQL com EXCEPTION WHEN OTHERS
    e reproduzido com um savepoint (begin_nested): qualquer erro dentro do
    bloco protegido (validacao, fn_saldo_cliente ou a query principal) e
    capturado, registrado como WARNING e degrada para a linha de fallback,
    preservando o controle de fluxo original (o erro NAO e re-lancado).
    '''
    v_saldo_atual: Decimal | None = None

    try:
        async with conn.begin_nested():
            if p_data_inicio > p_data_fim:
                raise PeriodoInvalidoError(p_data_inicio, p_data_fim)

            row = (
                await conn.execute(_SALDO_SQL, {'p_cliente_id': p_cliente_id})
            ).one()
            v_saldo_atual = _q2(Decimal(str(row.saldo)))
            logger.info(
                'Saldo atual do cliente %s: %s', p_cliente_id, v_saldo_atual
            )

            result = await conn.execute(
                _RELATORIO_SQL,
                {
                    'p_cliente_id': p_cliente_id,
                    'p_data_inicio': p_data_inicio,
                    'p_data_fim': p_data_fim,
                    'v_saldo_atual': v_saldo_atual,
                },
            )
            rows = [
                RelatorioMensalRow(
                    mes_referencia=r.mes_referencia,
                    total_creditos=_q2(Decimal(str(r.total_creditos))),
                    total_debitos=_q2(Decimal(str(r.total_debitos))),
                    saldo_consolidado=_q2(Decimal(str(r.saldo_consolidado))),
                    qtd_transacoes=int(r.qtd_transacoes),
                )
                for r in result.mappings()
            ]
        return rows
    except Exception as exc:  # noqa: BLE8 - reproduz WHEN OTHERS do PL/pgSQL
        logger.warning(
            'Falha ao gerar relatorio: %s. Retornando linha de fallback.', exc
        )
        fb = (
            await conn.execute(
                _FALLBACK_SQL,
                {'p_data_inicio': p_data_inicio, 'v_saldo_atual': v_saldo_atual},
            )
        ).one()
        return [
            RelatorioMensalRow(
                mes_referencia=fb.mes_referencia,
                total_creditos=Decimal(str(fb.total_creditos)),
                total_debitos=Decimal(str(fb.total_debitos)),
                saldo_consolidado=_q2(Decimal(str(fb.saldo_consolidado))),
                qtd_transacoes=int(fb.qtd_transacoes),
            )
        ]
