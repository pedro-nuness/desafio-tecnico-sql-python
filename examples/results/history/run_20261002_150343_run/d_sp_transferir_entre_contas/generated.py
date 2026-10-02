"""Python 3.14 port of PL/pgSQL procedure sp_transferir_entre_contas.

Transfers a value between two accounts atomically: validates balance and
account status, debits the origin, credits the destination, records the
transaction and writes an audit log entry. On any error, an error audit
entry is written and the exception is re-raised.

The caller owns the transaction: pass an open AsyncConnection; this module
never commits or rolls back the outer transaction.
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
    p_valor: Decimal,
) -> None:
    """Transfer ``p_valor`` from account ``p_conta_origem`` to ``p_conta_destino``.

    Mirrors the PL/pgSQL procedure, including the WHEN OTHERS handler: the
    whole body runs inside a SAVEPOINT (``begin_nested``); on any error the
    savepoint is rolled back, an error audit row is inserted and the
    exception is re-raised.
    """
    try:
        # PL/pgSQL BEGIN ... EXCEPTION WHEN OTHERS END == a savepoint.
        async with conn.begin_nested():
            # L20: IF p_valor IS NULL OR p_valor <= 0
            if p_valor is None or p_valor <= 0:
                raise ValorInvalidoError(
                    f"Valor invalido para transferencia: {p_valor}"
                )

            # L24: IF p_conta_origem = p_conta_destino
            if p_conta_origem == p_conta_destino:
                raise ContasIguaisError(
                    "Conta de origem e destino nao podem ser iguais"
                )

            # L28: lock origin row and read saldo/status (FOR UPDATE).
            row_origem = (
                await conn.execute(
                    text(
                        "SELECT saldo, status FROM contas "
                        "WHERE id = :p_conta_origem FOR UPDATE"
                    ),
                    {"p_conta_origem": p_conta_origem},
                )
            ).first()
            v_saldo_origem: Decimal | None = (
                row_origem[0] if row_origem is not None else None
            )
            v_status_origem: str | None = (
                row_origem[1] if row_origem is not None else None
            )

            # L31: lock destination row and read status (FOR UPDATE).
            row_destino = (
                await conn.execute(
                    text(
                        "SELECT status FROM contas "
                        "WHERE id = :p_conta_destino FOR UPDATE"
                    ),
                    {"p_conta_destino": p_conta_destino},
                )
            ).first()
            v_status_destino: str | None = (
                row_destino[0] if row_destino is not None else None
            )

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
                    f"Saldo insuficiente: saldo={v_saldo_origem} valor={p_valor}"
                )

            # L46-L47: debit origin, credit destination.
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

            # L49: record the transaction.
            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (:p_conta_origem, :p_conta_destino, "
                    "'TRANSFERENCIA', :p_valor)"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )

            # L52: success audit log (jsonb values cast to their own types
            # so the JSON keeps BIGINT/NUMERIC semantics).
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
    except Exception as exc:
        # WHEN OTHERS handler: the savepoint was already rolled back by the
        # ``async with`` above, so the transaction is usable again. Log the
        # error audit row, then re-raise (PL/pgSQL RAISE).
        erro_msg: str = (
            str(getattr(exc, "orig", None))
            if getattr(exc, "orig", None) is not None
            else str(exc)
        )
        logger.warning(
            "sp_transferir_entre_contas falhou: %s", erro_msg
        )
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
                "erro": erro_msg,
            },
        )
        raise
    else:
        logger.info(
            "Transferencia efetivada: origem=%s destino=%s valor=%s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
        )


def _unused_type_guard(value: Any) -> None:  # pragma: no cover
    """Keeps ``Any`` import referenced for typing of dynamic DB values."""
    _ = value
