# Resultados — Anexos B a F

Modelo: `openrouter/z-ai/glm-5.3-flash` · schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.

Prompt: `generation-v4`. Equivalência: **4/5 rotinas (80%)**; **20/21 casos (95.2%)**. Só holdout (casos nunca mostrados ao LLM): **4/5 rotinas, 8/9 casos (88.9%)**. Validade estática (AST): 100%; conclusão: 100%.

| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff | tokens in/out (última) | duração total | equivalência | holdout |
|---|---|---|---|---|---|---|---|---|---|
| [b_fn_saldo_cliente](b_fn_saldo_cliente/) | success | 1 | database_delegated / database_delegated | — | ok | 2109 / 638 | 16.1s | [3/3 (100%)](b_fn_saldo_cliente/evaluation.json) | 1/1 |
| [c_sp_atualizar_status_contas_inativas](c_sp_atualizar_status_contas_inativas/) | success | 2 | hybrid / hybrid | DIAGNOSTICS_SEMANTICS | ok | 3429 / 1349 | 16.3s | [4/4 (100%)](c_sp_atualizar_status_contas_inativas/evaluation.json) | 2/2 |
| [d_sp_transferir_entre_contas](d_sp_transferir_entre_contas/) | success | 1 | hybrid / hybrid | ROW_LOCKING | ok | 3655 / 3070 | 22.8s | [6/7 (86%)](d_sp_transferir_entre_contas/evaluation.json) | 2/3 |
| [e_sp_processar_lote_taxas](e_sp_processar_lote_taxas/) | success | 1 | hybrid / hybrid | N_PLUS_ONE | ok | 3914 / 4172 | 50.8s | [3/3 (100%)](e_sp_processar_lote_taxas/evaluation.json) | 1/1 |
| [f_sp_relatorio_mensal_cliente](f_sp_relatorio_mensal_cliente/) | success | 2 | hybrid / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, RECURSIVE_CTE, SWALLOWED_EXCEPTION | ok | 6026 / 7101 | 99.3s | [4/4 (100%)](f_sp_relatorio_mensal_cliente/evaluation.json) | 2/2 |

## Divergências comportamentais

- `d_sp_transferir_entre_contas` · unknown origin (holdout): generated code crashed instead of raising its own error: NoResultFound: No row was found when one was required
