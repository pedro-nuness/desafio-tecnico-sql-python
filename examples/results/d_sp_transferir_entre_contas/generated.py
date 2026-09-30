"""Modernized port of PL/pgSQL procedure sp_transferir_entre_contas.

Transfers a value between two accounts atomically, validating balance and
status, recording the transaction and an audit log entry. On failure, an
error audit entry is written (via a SAVEPOINT, mirroring the PL/pgSQL
exception subtransaction) and the original error is re-raised.

The caller owns the outer transaction: pass an open AsyncConnection.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base exception for account transfer failures."""


class ValorInvalidoError(TransferenciaError):
    """Raised when the transfer value is null or non-positive."""


class ContasIguaisError(TransferenciaError):
    """Raised when origin and destination accounts are the same."""


class ContaOrigemNaoEncontradaError(TransferenciaError):
    """Raised when the origin account does not exist."""


class ContasNaoAtivasError(TransferenciaError):
    """Raised when either account is not in 'ATIVA' status."""


class SaldoInsuficienteError(TransferenciaError):
    """Raised when the origin account lacks sufficient balance."""


async def sp_transferir_entre_contas(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal | None,
) -> None:
    """Transfer ``p_valor`` from ``p_conta_origem`` to ``p_conta_destino``.

    Mirrors the legacy procedure: locks both accounts (FOR UPDATE), validates
    value/status/balance, performs the balance updates, inserts the
    transaction record and a success audit entry. On any failure, an error
    audit entry is written inside a SAVEPOINT (like the PL/pgSQL exception
    subtransaction) and the error is re-raised.

    The caller owns the outer transaction; no commit/rollback is issued here
    (the SAVEPOINT below is an internal subtransaction, not a commit).
    """
    # L20: value validation
    if p_valor is None or p_valor <= 0:
        raise ValorInvalidoError(f"Valor invalido para transferencia: {p_valor}")

    # L24: same-account validation
    if p_conta_origem == p_conta_destino:
        raise ContasIguaisError("Conta de origem e destino nao podem ser iguais")

    # PL/pgSQL EXCEPTION block = implicit subtransaction -> SAVEPOINT.
    nested = conn.begin_nested()
    try:
        # L28: lock origin account and read saldo/status
        row_origem = (
            await conn.execute(
                text(
                    "SELECT saldo, status FROM contas "
                    "WHERE id = :p_conta_origem FOR UPDATE"
                ),
                {"p_conta_origem": p_conta_origem},
            )
        ).first()
        v_saldo_origem: Decimal | None = row_origem[0] if row_origem else None
        v_status_origem: str | None = row_origem[1] if row_origem else None

        # L31: lock destination account and read status
        row_destino = (
            await conn.execute(
                text(
                    "SELECT status FROM contas "
                    "WHERE id = :p_conta_destino FOR UPDATE"
                ),
                {"p_conta_destino": p_conta_destino},
            )
        ).first()
        v_status_destino: str | None = row_destino[0] if row_destino else None

        # L34: origin must exist
        if v_saldo_origem is None:
            raise ContaOrigemNaoEncontradaError(
                f"Conta de origem {p_conta_origem} nao encontrada"
            )

        # L38: both accounts must be active
        if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
            raise ContasNaoAtivasError("Ambas as contas precisam estar ATIVAS")

        # L42: sufficient balance
        if v_saldo_origem < p_valor:
            raise SaldoInsuficienteError(
                f"Saldo insuficiente: saldo={v_saldo_origem} valor={p_valor}"
            )

        # L46-L47: balance updates (row locks and writes share this connection)
        await conn.execute(
            text(
                "UPDATE contas SET saldo = saldo - :p_valor "
                "WHERE id = :p_conta_origem"
            ),
            {"p_valor": p_valor, "p_conta_origem": p_conta_origem},
        )
        await conn.execute(
            text(
                "UPDATE contas SET saldo = saldo + :p_valor "
                "WHERE id = :p_conta_destino"
            ),
            {"p_valor": p_valor, "p_conta_destino": p_conta_destino},
        )

        # L49: transaction record
        await conn.execute(
            text(
                "INSERT INTO transacoes "
                "(conta_origem_id, conta_destino_id, tipo, valor) "
                "VALUES (:p_conta_origem, :p_conta_destino, 'TRANSFERENCIA', :p_valor)"
            ),
            {
                "p_conta_origem": p_conta_origem,
                "p_conta_destino": p_conta_destino,
                "p_valor": p_valor,
            },
        )

        # L52: success audit entry (jsonb_build_object kept in SQL)
        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes) "
                "VALUES ('transacoes', NULL, 'TRANSFERENCIA_OK', "
                "jsonb_build_object("
                "'origem',  CAST(:p_conta_origem AS bigint), "
                "'destino', CAST(:p_conta_destino AS bigint), "
                "'valor',   CAST(:p_valor AS numeric))"
            ),
            {
                "p_conta_origem": p_conta_origem,
                "p_conta_destino": p_conta_destino,
                "p_valor": p_valor,
            },
        )

        await nested.commit()

    except (TransferenciaError, DBAPIError, Exception) as exc:
        # L66: error audit entry, mirroring WHEN OTHERS THEN ... RAISE.
        # The savepoint is rolled back so the failed statements do not poison
        # the outer transaction, then the error log is written and the error
        # re-raised (SQLERRM -> str(exc)).
        await nested.rollback()
        erro_msg: str = str(exc)
        logger.warning(
            "Transferencia falhou (origem=%s destino=%s valor=%s): %s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            erro_msg,
        )
        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, acao, detalhes) "
                "VALUES ('transacoes', 'TRANSFERENCIA_ERRO', "
                "jsonb_build_object("
                "'origem',  CAST(:p_conta_origem AS bigint), "
                "'destino', CAST(:p_conta_destino AS bigint), "
                "'valor',   CAST(:p_valor AS numeric), "
                "'erro',    CAST(:erro AS text))"
            ),
            {
                "p_conta_origem": p_conta_origem,
                "p_conta_destino": p_conta_destino,
                "p_valor": p_valor,
                "erro": erro_msg,
            },
        )
        raise
