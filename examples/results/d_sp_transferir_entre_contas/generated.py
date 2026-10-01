"""Modernization of PL/pgSQL procedure sp_transferir_entre_contas.

Transfers a value between two accounts atomically: validates balance and
account status, records the transaction and an audit log entry. On any
error, an error audit entry is written and the exception is re-raised.

The caller owns the transaction: pass an open AsyncConnection; this module
never commits or rolls back the outer transaction. The original
EXCEPTION ... WHEN OTHERS block is reproduced with a SAVEPOINT
(begin_nested) so the transaction stays usable for the error-log insert.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base exception mapped from RAISE EXCEPTION in the original routine."""


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
    """Transfer ``p_valor`` from ``p_conta_origem`` to ``p_conta_destino``.

    Mirrors the original procedure: validations, FOR UPDATE row locks,
    balance updates, transaction record and success audit log. Any error
    triggers an error audit log entry (after savepoint rollback) and the
    exception is re-raised, as in the original ``RAISE``.
    """
    try:
        # BEGIN ... EXCEPTION block == savepoint: protected statements run
        # inside begin_nested; the handler runs after it was rolled back.
        async with conn.begin_nested():
            if p_valor is None or p_valor <= Decimal("0"):
                raise ValorInvalidoError(
                    f"Valor invalido para transferencia: {p_valor}"
                )

            if p_conta_origem == p_conta_destino:
                raise ContasIguaisError(
                    "Conta de origem e destino nao podem ser iguais"
                )

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

            row_dest = (
                await conn.execute(
                    text(
                        "SELECT status FROM contas "
                        "WHERE id = :p_conta_destino FOR UPDATE"
                    ),
                    {"p_conta_destino": p_conta_destino},
                )
            ).first()
            v_status_destino: str | None = (
                row_dest[0] if row_dest is not None else None
            )

            if v_saldo_origem is None:
                raise ContaOrigemNaoEncontradaError(
                    f"Conta de origem {p_conta_origem} nao encontrada"
                )

            if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
                raise ContasNaoAtivasError(
                    "Ambas as contas precisam estar ATIVAS"
                )

            if v_saldo_origem < p_valor:
                raise SaldoInsuficienteError(
                    f"Saldo insuficiente: saldo={v_saldo_origem} "
                    f"valor={p_valor}"
                )

            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo - CAST(:p_valor AS NUMERIC(18,2)) "
                    "WHERE id = :p_conta_origem"
                ),
                {
                    "p_valor": p_valor,
                    "p_conta_origem": p_conta_origem,
                },
            )
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo + CAST(:p_valor AS NUMERIC(18,2)) "
                    "WHERE id = :p_conta_destino"
                ),
                {
                    "p_valor": p_valor,
                    "p_conta_destino": p_conta_destino,
                },
            )

            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (:p_conta_origem, :p_conta_destino, "
                    "'TRANSFERENCIA', CAST(:p_valor AS NUMERIC(18,2)))"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )

            await conn.execute(
                text(
                    "INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes) "
                    "VALUES ('transacoes', NULL, 'TRANSFERENCIA_OK', "
                    "jsonb_build_object("
                    "'origem', CAST(:p_conta_origem AS BIGINT), "
                    "'destino', CAST(:p_conta_destino AS BIGINT), "
                    "'valor', CAST(:p_valor AS NUMERIC(18,2))))"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )
    except TransferenciaError as exc:
        # Savepoint already rolled back by begin_nested context exit;
        # transaction is usable again for the error audit log.
        await _registrar_erro(conn, p_conta_origem, p_conta_destino, p_valor, str(exc))
        raise
    except Exception as exc:
        # WHEN OTHERS: any other error also logs and re-raises.
        await _registrar_erro(conn, p_conta_origem, p_conta_destino, p_valor, str(exc))
        raise TransferenciaError(str(exc)) from exc


async def _registrar_erro(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal | None,
    erro: str,
) -> None:
    """Insert the TRANSFERENCIA_ERRO audit entry (SQLERRM equivalent)."""
    await conn.execute(
        text(
            "INSERT INTO log_auditoria (entidade, acao, detalhes) "
            "VALUES ('transacoes', 'TRANSFERENCIA_ERRO', "
            "jsonb_build_object("
            "'origem', CAST(:p_conta_origem AS BIGINT), "
            "'destino', CAST(:p_conta_destino AS BIGINT), "
            "'valor', CAST(:p_valor AS NUMERIC(18,2)), "
            "'erro', CAST(:erro AS TEXT)))"
        ),
        {
            "p_conta_origem": p_conta_origem,
            "p_conta_destino": p_conta_destino,
            "p_valor": p_valor,
            "erro": erro,
        },
    )
