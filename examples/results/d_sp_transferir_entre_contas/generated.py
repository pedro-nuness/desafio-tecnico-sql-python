from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransferenciaError(Exception):
    """Base para erros de sp_transferir_entre_contas."""


class ValorInvalidoError(TransferenciaError):
    """Valor invalido para transferencia."""


class ContasIguaisError(TransferenciaError):
    """Conta de origem e destino nao podem ser iguais."""


class ContaOrigemNaoEncontradaError(TransferenciaError):
    """Conta de origem nao encontrada."""


class ContasNaoAtivasError(TransferenciaError):
    """Ambas as contas precisam estar ATIVAS."""


class SaldoInsuficienteError(TransferenciaError):
    """Saldo insuficiente."""


async def sp_transferir_entre_contas(
    conn: AsyncConnection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal | None,
) -> None:
    """Transfere um valor entre duas contas de forma atomica.

    O chamador possui a transacao externa. Internamente um SAVEPOINT
    (begin_nested) reproduz o subtransaction implicito do bloco
    EXCEPTION do PL/pgSQL: falhas de DML sao revertidas, mas o registro
    de auditoria de erro e persistido, e o erro e re-lancado.
    """
    if p_valor is None or p_valor <= 0:
        raise ValorInvalidoError(f"Valor invalido para transferencia: {p_valor}")

    if p_conta_origem == p_conta_destino:
        raise ContasIguaisError("Conta de origem e destino nao podem ser iguais")

    nested = conn.begin_nested()
    try:
        row = (
            await conn.execute(
                text(
                    "SELECT saldo, status FROM contas "
                    "WHERE id = :conta_origem FOR UPDATE"
                ),
                {"conta_origem": p_conta_origem},
            )
        ).first()

        v_saldo_origem: Decimal | None = row[0] if row is not None else None
        v_status_origem: str | None = row[1] if row is not None else None

        row_dest = (
            await conn.execute(
                text(
                    "SELECT status FROM contas "
                    "WHERE id = :conta_destino FOR UPDATE"
                ),
                {"conta_destino": p_conta_destino},
            )
        ).first()
        v_status_destino: str | None = row_dest[0] if row_dest is not None else None

        if v_saldo_origem is None:
            raise ContaOrigemNaoEncontradaError(
                f"Conta de origem {p_conta_origem} nao encontrada"
            )

        if v_status_origem != "ATIVA" or v_status_destino != "ATIVA":
            raise ContasNaoAtivasError("Ambas as contas precisam estar ATIVAS")

        if v_saldo_origem < p_valor:
            raise SaldoInsuficienteError(
                f"Saldo insuficiente: saldo={v_saldo_origem} valor={p_valor}"
            )

        await conn.execute(
            text(
                "UPDATE contas SET saldo = saldo - :valor WHERE id = :conta_origem"
            ),
            {"valor": p_valor, "conta_origem": p_conta_origem},
        )
        await conn.execute(
            text(
                "UPDATE contas SET saldo = saldo + :valor WHERE id = :conta_destino"
            ),
            {"valor": p_valor, "conta_destino": p_conta_destino},
        )

        await conn.execute(
            text(
                "INSERT INTO transacoes "
                "(conta_origem_id, conta_destino_id, tipo, valor) "
                "VALUES (:conta_origem, :conta_destino, 'TRANSFERENCIA', :valor)"
            ),
            {
                "conta_origem": p_conta_origem,
                "conta_destino": p_conta_destino,
                "valor": p_valor,
            },
        )

        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes) "
                "VALUES ('transacoes', NULL, 'TRANSFERENCIA_OK', "
                "jsonb_build_object('origem', :conta_origem, "
                "'destino', :conta_destino, 'valor', :valor))"
            ),
            {
                "conta_origem": p_conta_origem,
                "conta_destino": p_conta_destino,
                "valor": p_valor,
            },
        )

        await nested.commit()
    except Exception as exc:
        await nested.rollback()
        erro = str(exc)
        logger.error("Transferencia falhou: %s", erro)
        await conn.execute(
            text(
                "INSERT INTO log_auditoria (entidade, acao, detalhes) "
                "VALUES ('transacoes', 'TRANSFERENCIA_ERRO', "
                "jsonb_build_object('origem', :conta_origem, "
                "'destino', :conta_destino, 'valor', :valor, 'erro', :erro))"
            ),
            {
                "conta_origem": p_conta_origem,
                "conta_destino": p_conta_destino,
                "valor": p_valor,
                "erro": erro,
            },
        )
        raise
