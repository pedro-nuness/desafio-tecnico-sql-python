"""Modernized port of PL/pgSQL procedure sp_transferir_entre_contas.

The caller owns the transaction: the AsyncConnection must have an active
transaction so the FOR UPDATE row locks and the dependent writes are atomic.

Important fidelity note: ``p_valor`` is declared NUMERIC (unconstrained) in the
original procedure. It must NOT be pre-rounded to NUMERIC(18,2) before the
``saldo - p_valor`` arithmetic: PostgreSQL computes the subtraction at full
precision and only the assignment to the NUMERIC(18,2) ``saldo`` column rounds
(half away from zero). Pre-casting the bind to NUMERIC(18,2) would round 0.005
to 0.01 first and produce a different balance.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base error for sp_transferir_entre_contas."""


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

    Mirrors the original PL/pgSQL procedure, including the audit-log-on-error
    block (implemented as a savepoint) followed by a re-raise.
    """
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

    # L28: SELECT saldo, status FROM contas WHERE id = p_conta_origem FOR UPDATE
    row_origem = (
        await conn.execute(
            text(
                "SELECT saldo, status FROM contas "
                "WHERE id = CAST(:p_conta_origem AS BIGINT) FOR UPDATE"
            ),
            {"p_conta_origem": p_conta_origem},
        )
    ).first()
    v_saldo_origem: Decimal | None = row_origem[0] if row_origem else None
    v_status_origem: str | None = row_origem[1] if row_origem else None

    # L31: SELECT status FROM contas WHERE id = p_conta_destino FOR UPDATE
    row_destino = (
        await conn.execute(
            text(
                "SELECT status FROM contas "
                "WHERE id = CAST(:p_conta_destino AS BIGINT) FOR UPDATE"
            ),
            {"p_conta_destino": p_conta_destino},
        )
    ).first()
    v_status_destino: str | None = row_destino[0] if row_destino else None

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

    try:
        # L46-L49: the protected writes (BEGIN ... EXCEPTION block = savepoint)
        async with conn.begin_nested():
            # p_valor stays at full NUMERIC precision here; the NUMERIC(18,2)
            # saldo column performs the final rounding on assignment, exactly
            # like the original procedure.
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo - CAST(:p_valor AS NUMERIC) "
                    "WHERE id = CAST(:p_conta_origem AS BIGINT)"
                ),
                {"p_valor": p_valor, "p_conta_origem": p_conta_origem},
            )
            await conn.execute(
                text(
                    "UPDATE contas SET saldo = saldo + CAST(:p_valor AS NUMERIC) "
                    "WHERE id = CAST(:p_conta_destino AS BIGINT)"
                ),
                {"p_valor": p_valor, "p_conta_destino": p_conta_destino},
            )
            await conn.execute(
                text(
                    "INSERT INTO transacoes "
                    "(conta_origem_id, conta_destino_id, tipo, valor) "
                    "VALUES (CAST(:p_conta_origem AS BIGINT), "
                    "CAST(:p_conta_destino AS BIGINT), 'TRANSFERENCIA', "
                    "CAST(:p_valor AS NUMERIC))"
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
                    "'valor', CAST(:p_valor AS NUMERIC)))"
                ),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                },
            )
    except Exception as exc:
        # L66: WHEN OTHERS handler runs after the savepoint rollback.
        sqlerrm = str(exc)
        logger.warning(
            "Transferencia falhou (origem=%s destino=%s valor=%s): %s",
            p_conta_origem,
            p_conta_destino,
            p_valor,
            sqlerrm,
        )
        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, acao, detalhes) "
                "VALUES ('transacoes', 'TRANSFERENCIA_ERRO', "
                "jsonb_build_object("
                "'origem', CAST(:p_conta_origem AS BIGINT), "
                "'destino', CAST(:p_conta_destino AS BIGINT), "
                "'valor', CAST(:p_valor AS NUMERIC), "
                "'erro', CAST(:sqlerrm AS TEXT)))"
            ),
            {
                "p_conta_origem": p_conta_origem,
                "p_conta_destino": p_conta_destino,
                "p_valor": p_valor,
                "sqlerrm": sqlerrm,
            },
        )
        # L77: RAISE (re-raise)
        raise
