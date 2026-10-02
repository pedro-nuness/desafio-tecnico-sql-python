"""Port of sp_transferir_entre_contas (PL/pgSQL procedure) to Python 3.14.

The caller owns the transaction: pass an open AsyncConnection. Row locks
(FOR UPDATE) and the dependent writes run on that same connection/transaction.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base for errors raised by sp_transferir_entre_contas."""


class ValorInvalidoError(TransferenciaError):
    """RAISE EXCEPTION 'Valor invalido para transferencia: %'."""

    def __init__(self, valor: Decimal | None) -> None:
        super().__init__(f"Valor invalido para transferencia: {valor}")
        self.valor = valor


class ContasIguaisError(TransferenciaError):
    """RAISE EXCEPTION 'Conta de origem e destino nao podem ser iguais'."""


class ContaOrigemNaoEncontradaError(TransferenciaError):
    """RAISE EXCEPTION 'Conta de origem % nao encontrada'."""

    def __init__(self, conta: int) -> None:
        super().__init__(f"Conta de origem {conta} nao encontrada")
        self.conta = conta


class ContasNaoAtivasError(TransferenciaError):
    """RAISE EXCEPTION 'Ambas as contas precisam estar ATIVAS'."""


class SaldoInsuficienteError(TransferenciaError):
    """RAISE EXCEPTION 'Saldo insuficiente: saldo=% valor=%'."""

    def __init__(self, saldo: Decimal, valor: Decimal) -> None:
        super().__init__(f"Saldo insuficiente: saldo={saldo} valor={valor}")
        self.saldo = saldo
        self.valor = valor


async def sp_transferir_entre_contas(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal,
) -> None:
    """Transfer a value between two accounts atomically.

    Validates balance and account status, locks both account rows, performs
    the two updates, records the transaction and writes an audit log entry.
    On any failure, an error audit row is written (after rolling back the
    savepoint) and the original exception is re-raised, mirroring the
    PL/pgSQL EXCEPTION WHEN OTHERS ... RAISE block.
    """
    try:
        # The whole BEGIN ... EXCEPTION block runs inside a savepoint so that
        # a failure can be logged on a non-aborted transaction (rule 14).
        async with conn.begin_nested():
            if p_valor is None or p_valor <= 0:
                raise ValorInvalidoError(p_valor)

            if p_conta_origem == p_conta_destino:
                raise ContasIguaisError()

            row = (
                await conn.execute(
                    text(
                        "SELECT saldo, status FROM contas "
                        "WHERE id = :p_conta_origem FOR UPDATE"
                    ),
                    {"p_conta_origem": p_conta_origem},
                )
            ).first()
            v_saldo_origem: Decimal | None = row.saldo if row else None
            v_status_origem: str | None = row.status if row else None

            row_dest = (
                await conn.execute(
                    text(
                        "SELECT status FROM contas "
                        "WHERE id = :p_conta_destino FOR UPDATE"
                    ),
                    {"p_conta_destino": p_conta_destino},
                )
            ).first()
            v_status_destino: str | None = row_dest.status if row_dest else None

            if v_saldo_origem is None:
                raise ContaOrigemNaoEncontradaError(p_conta_origem)

            if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
                raise ContasNaoAtivasError()

            if v_saldo_origem < p_valor:
                raise SaldoInsuficienteError(v_saldo_origem, p_valor)

            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo - CAST(:p_valor AS NUMERIC(18,2)) "
                    "WHERE id = :p_conta_origem"
                ),
                {"p_valor": p_valor, "p_conta_origem": p_conta_origem},
            )
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo + CAST(:p_valor AS NUMERIC(18,2)) "
                    "WHERE id = :p_conta_destino"
                ),
                {"p_valor": p_valor, "p_conta_destino": p_conta_destino},
            )

            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (:p_conta_origem, :p_conta_destino, 'TRANSFERENCIA', "
                    "CAST(:p_valor AS NUMERIC(18,2)))"
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
    except Exception as exc:  # WHEN OTHERS
        # Savepoint was rolled back by begin_nested(); the transaction is
        # usable again, so the error audit insert can proceed (SQLERRM).
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
                "erro": str(exc),
            },
        )
        logger.warning(
            "Transferencia falhou: origem=%s destino=%s valor=%s erro=%s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            exc,
        )
        raise  # PL/pgSQL RAISE (re-raise)
