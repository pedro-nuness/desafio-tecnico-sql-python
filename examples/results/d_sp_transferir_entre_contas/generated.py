"""Python 3.14 port of PL/pgSQL procedure sp_transferir_entre_contas.

Transfers a value between two accounts atomically: validates balance and
account status, writes the transaction row and an audit log entry. On any
error, an error audit row is written and the original exception re-raised.

The caller owns the transaction: pass an open AsyncConnection; this module
never commits or rolls back the outer transaction. The original
EXCEPTION WHEN OTHERS block is reproduced with a nested savepoint
(`begin_nested`) so the failed statements are rolled back while the
connection stays usable for the error-log insert.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base error for sp_transferir_entre_contas (RAISE EXCEPTION)."""


class ValorInvalidoError(TransferenciaError):
    """p_valor is NULL or <= 0."""


class ContasIguaisError(TransferenciaError):
    """Origin and destination accounts are the same."""


class ContaOrigemNaoEncontradaError(TransferenciaError):
    """Origin account does not exist."""


class ContasNaoAtivasError(TransferenciaError):
    """Both accounts must be ATIVA."""


class SaldoInsuficienteError(TransferenciaError):
    """Insufficient balance in the origin account."""


async def sp_transferir_entre_contas(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal,
) -> None:
    """Transfer ``p_valor`` from account ``p_conta_origem`` to
    ``p_conta_destino``. Raises a TransferenciaError subclass on any
    validation failure; the error is also recorded in log_auditoria.
    """
    # The original procedure wraps its whole body in an implicit
    # BEGIN ... EXCEPTION WHEN OTHERS block, which in PostgreSQL is a
    # savepoint. Reproduce it: everything runs inside begin_nested(); on
    # error the savepoint is rolled back, the error audit row is inserted,
    # and the exception is re-raised.
    try:
        async with conn.begin_nested():
            if p_valor is None or p_valor <= 0:
                raise ValorInvalidoError(
                    f"Valor invalido para transferencia: {p_valor}"
                )

            if p_conta_origem == p_conta_destino:
                raise ContasIguaisError(
                    "Conta de origem e destino nao podem ser iguais"
                )

            # Lock origin row (FOR UPDATE) and read saldo/status.
            row = (
                await conn.execute(
                    text(
                        "SELECT saldo, status FROM contas "
                        "WHERE id = :p_conta_origem FOR UPDATE"
                    ),
                    {"p_conta_origem": p_conta_origem},
                )
            ).first()

            v_saldo_origem: Decimal | None = row[0] if row else None
            v_status_origem: str | None = row[1] if row else None

            # Lock destination row and read status.
            row_dest = (
                await conn.execute(
                    text(
                        "SELECT status FROM contas "
                        "WHERE id = :p_conta_destino FOR UPDATE"
                    ),
                    {"p_conta_destino": p_conta_destino},
                )
            ).first()

            v_status_destino: str | None = row_dest[0] if row_dest else None

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
    except Exception as exc:
        # Savepoint already rolled back by begin_nested() context manager;
        # the connection is usable again for the error audit insert.
        erro = str(exc)
        logger.warning(
            "sp_transferir_entre_contas falhou: origem=%s destino=%s valor=%s erro=%s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            erro,
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
                "erro": erro,
            },
        )
        raise
