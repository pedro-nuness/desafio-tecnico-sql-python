"""Python 3.14 port of the PL/pgSQL procedure sp_transferir_entre_contas.

The caller owns the transaction: the connection must be passed in with an
open transaction, since the FOR UPDATE row locks and the writes that depend
on them must share the same transaction.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base for domain errors raised by sp_transferir_entre_contas."""


class ValorInvalidoError(TransferenciaError):
    """RAISE EXCEPTION 'Valor invalido para transferencia: %'."""


class ContasIguaisError(TransferenciaError):
    """RAISE EXCEPTION 'Conta de origem e destino nao podem ser iguais'."""


class ContaOrigemNaoEncontradaError(TransferenciaError):
    """RAISE EXCEPTION 'Conta de origem % nao encontrada'."""


class ContasNaoAtivasError(TransferenciaError):
    """RAISE EXCEPTION 'Ambas as contas precisam estar ATIVAS'."""


class SaldoInsuficienteError(TransferenciaError):
    """RAISE EXCEPTION 'Saldo insuficiente: saldo=% valor=%'."""


async def sp_transferir_entre_contas(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal,
) -> None:
    """Transfer ``p_valor`` between two accounts, mirroring the original procedure."""
    # L20: IF p_valor IS NULL OR p_valor <= 0
    if p_valor is None or p_valor <= 0:
        raise ValorInvalidoError(f"Valor invalido para transferencia: {p_valor}")

    # L24: IF p_conta_origem = p_conta_destino
    if p_conta_origem == p_conta_destino:
        raise ContasIguaisError("Conta de origem e destino nao podem ser iguais")

    # L28: SELECT saldo, status FROM contas WHERE id = p_conta_origem FOR UPDATE
    row = (
        await conn.execute(
            text(
                "SELECT saldo, status FROM contas "
                "WHERE id = :p_conta_origem FOR UPDATE"
            ),
            {"p_conta_origem": p_conta_origem},
        )
    ).first()
    v_saldo_origem: Decimal | None = row[0] if row is not None else None
    v_status_origem: str | None = row[1] if row is not None else None

    # L31: SELECT status FROM contas WHERE id = p_conta_destino FOR UPDATE
    v_status_destino: str | None = (
        await conn.execute(
            text(
                "SELECT status FROM contas "
                "WHERE id = :p_conta_destino FOR UPDATE"
            ),
            {"p_conta_destino": p_conta_destino},
        )
    ).scalar_one_or_none()

    # L34: IF v_saldo_origem IS NULL
    if v_saldo_origem is None:
        raise ContaOrigemNaoEncontradaError(
            f"Conta de origem {p_conta_origem} nao encontrada"
        )

    # L38: IF v_status_origem <> 'ATIVA' OR v_status_destino <> 'ATIVA'
    if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
        raise ContasNaoAtivasError("Ambas as contas precisam estar ATIVAS")

    # L42: IF v_saldo_origem < p_valor
    if v_saldo_origem < p_valor:
        raise SaldoInsuficienteError(
            f"Saldo insuficiente: saldo={v_saldo_origem} valor={p_valor}"
        )

    # L52..L77: BEGIN ... EXCEPTION WHEN OTHERS block -> savepoint.
    # The protected statements run inside the savepoint; on any error the
    # savepoint is rolled back, the audit insert runs on a clean transaction,
    # and the error is re-raised (original 're-raise' behaviour).
    try:
        async with conn.begin_nested():
            # L46
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo - :p_valor "
                    "WHERE id = :p_conta_origem"
                ),
                {
                    "p_valor": p_valor,
                    "p_conta_origem": p_conta_origem,
                },
            )
            # L47
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo + :p_valor "
                    "WHERE id = :p_conta_destino"
                ),
                {
                    "p_valor": p_valor,
                    "p_conta_destino": p_conta_destino,
                },
            )
            # L49
            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (:p_conta_origem, :p_conta_destino, "
                    "'TRANSFERENCIA', CAST(:p_valor AS NUMERIC))"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )
            # L52
            await conn.execute(
                text(
                    "INSERT INTO log_auditoria "
                    "(entidade, entidade_id, acao, detalhes) VALUES ("
                    "'transacoes', NULL, 'TRANSFERENCIA_OK', "
                    "jsonb_build_object("
                    "'origem', CAST(:p_conta_origem AS BIGINT), "
                    "'destino', CAST(:p_conta_destino AS BIGINT), "
                    "'valor', CAST(:p_valor AS NUMERIC))"
                    ")"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )
    except Exception as exc:  # WHEN OTHERS
        logger.error(
            "sp_transferir_entre_contas falhou: origem=%s destino=%s valor=%s erro=%s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            exc,
        )
        # L66: audit insert with SQLERRM, executed after the savepoint rollback
        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, acao, detalhes) VALUES ("
                "'transacoes', 'TRANSFERENCIA_ERRO', "
                "jsonb_build_object("
                "'origem', CAST(:p_conta_origem AS BIGINT), "
                "'destino', CAST(:p_conta_destino AS BIGINT), "
                "'valor', CAST(:p_valor AS NUMERIC), "
                "'erro', CAST(:erro AS TEXT))"
                ")"
            ),
            {
                "p_conta_origem": p_conta_origem,
                "p_conta_destino": p_conta_destino,
                "p_valor": p_valor,
                "erro": str(exc),
            },
        )
        # L77: RAISE EXCEPTION 're-raise'
        raise
