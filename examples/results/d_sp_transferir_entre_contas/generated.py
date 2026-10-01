"""Python 3.14 port of the PL/pgSQL procedure sp_transferir_entre_contas.

Transfers a value between two accounts atomically: validates balance and
account status, writes the transaction and an audit log entry. On any
failure, writes a TRANSFERENCIA_ERRO audit row and re-raises.

The caller owns the transaction: an open AsyncConnection with an active
transaction is expected; this module never commits or rolls back the outer
transaction (savepoints via begin_nested are used to reproduce the
PL/pgSQL EXCEPTION block semantics).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base exception for sp_transferir_entre_contas failures."""


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
    p_valor: Decimal | None,
) -> None:
    """Transfer ``p_valor`` from account ``p_conta_origem`` to
    ``p_conta_destino``.

    Mirrors the original procedure: every failure (including the Python-side
    validations) is caught, logged to ``log_auditoria`` as
    TRANSFERENCIA_ERRO inside a savepoint, and re-raised.
    """
    try:
        # The whole body runs inside a savepoint: in PL/pgSQL the EXCEPTION
        # block wraps everything, so even a validation RAISE rolls back any
        # work done so far before the error-log insert.
        async with conn.begin_nested():
            # L20: IF p_valor IS NULL OR p_valor <= 0
            if p_valor is None or p_valor <= Decimal("0"):
                raise ValorInvalidoError(
                    f"Valor invalido para transferencia: {p_valor}"
                )

            # L24: IF p_conta_origem = p_conta_destino
            if p_conta_origem == p_conta_destino:
                raise ContasIguaisError(
                    "Conta de origem e destino nao podem ser iguais"
                )

            # L28: SELECT saldo, status ... FOR UPDATE (locks origem row)
            row = (
                await conn.execute(
                    text(
                        "SELECT saldo, status FROM contas "
                        "WHERE id = CAST(:id_origem AS BIGINT) FOR UPDATE"
                    ),
                    {"id_origem": p_conta_origem},
                )
            ).one()
            v_saldo_origem: Decimal | None = row[0]
            v_status_origem: str | None = row[1]

            # L31: SELECT status ... FOR UPDATE (locks destino row)
            v_status_destino: str | None = (
                await conn.execute(
                    text(
                        "SELECT status FROM contas "
                        "WHERE id = CAST(:id_destino AS BIGINT) FOR UPDATE"
                    ),
                    {"id_destino": p_conta_destino},
                )
            ).scalar_one()

            # L34: IF v_saldo_origem IS NULL
            if v_saldo_origem is None:
                raise ContaOrigemNaoEncontradaError(
                    f"Conta de origem {p_conta_origem} nao encontrada"
                )

            # L38: IF v_status_origem <> 'ATIVA' OR v_status_destino <> 'ATIVA'
            if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
                raise ContasNaoAtivasError(
                    "Ambas as contas precisam estar ATIVAS"
                )

            # L42: IF v_saldo_origem < p_valor
            if v_saldo_origem < p_valor:
                raise SaldoInsuficienteError(
                    f"Saldo insuficiente: saldo={v_saldo_origem} "
                    f"valor={p_valor}"
                )

            # L46/L47: debit origem, credit destino (rows already locked on
            # this same connection/transaction).
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo "
                    "- CAST(:valor AS NUMERIC(18,2)) "
                    "WHERE id = CAST(:id_origem AS BIGINT)"
                ),
                {"valor": p_valor, "id_origem": p_conta_origem},
            )
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo "
                    "+ CAST(:valor AS NUMERIC(18,2)) "
                    "WHERE id = CAST(:id_destino AS BIGINT)"
                ),
                {"valor": p_valor, "id_destino": p_conta_destino},
            )

            # L49: record the transaction
            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (CAST(:id_origem AS BIGINT), "
                    "CAST(:id_destino AS BIGINT), 'TRANSFERENCIA', "
                    "CAST(:valor AS NUMERIC(18,2)))"
                ),
                {
                    "id_origem": p_conta_origem,
                    "id_destino": p_conta_destino,
                    "valor": p_valor,
                },
            )

            # L52: success audit log
            await conn.execute(
                text(
                    "INSERT INTO log_auditoria "
                    "(entidade, entidade_id, acao, detalhes) VALUES "
                    "('transacoes', NULL, 'TRANSFERENCIA_OK', "
                    "jsonb_build_object("
                    "'origem', CAST(:id_origem AS BIGINT), "
                    "'destino', CAST(:id_destino AS BIGINT), "
                    "'valor', CAST(:valor AS NUMERIC(18,2))))"
                ),
                {
                    "id_origem": p_conta_origem,
                    "id_destino": p_conta_destino,
                    "valor": p_valor,
                },
            )
    except Exception as exc:  # WHEN OTHERS THEN
        erro: str = str(exc)
        logger.error(
            "sp_transferir_entre_contas falhou: origem=%s destino=%s "
            "valor=%s erro=%s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            erro,
        )
        # The error-log insert runs on its own savepoint so that, if it
        # fails, its error propagates exactly like a failure inside the
        # PL/pgSQL handler would.
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO log_auditoria (entidade, acao, detalhes) "
                    "VALUES ('transacoes', 'TRANSFERENCIA_ERRO', "
                    "jsonb_build_object("
                    "'origem', CAST(:id_origem AS BIGINT), "
                    "'destino', CAST(:id_destino AS BIGINT), "
                    "'valor', CAST(:valor AS NUMERIC(18,2)), "
                    "'erro', CAST(:erro AS TEXT)))"
                ),
                {
                    "id_origem": p_conta_origem,
                    "id_destino": p_conta_destino,
                    "valor": p_valor,
                    "erro": erro,
                },
            )
        raise


__all__ = [
    "ContasIguaisError",
    "ContasNaoAtivasError",
    "ContaOrigemNaoEncontradaError",
    "SaldoInsuficienteError",
    "TransferenciaError",
    "ValorInvalidoError",
    "sp_transferir_entre_contas",
]

# Unused-import guard for typing.Any (kept out intentionally).
_ = Any
