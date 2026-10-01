# Resultados — Anexos B a F

Modelo: `openrouter/z-ai/glm-5.3-flash` · schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.

Prompt: `generation-v4`. Equivalência: **5/5 rotinas (100%)**; **21/21 casos (100.0%)**. Só holdout (casos nunca mostrados ao LLM): **5/5 rotinas, 9/9 casos (100.0%)**. Validade estática (AST): 100%; conclusão: 100%.

| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff | tokens in/out (última) | duração total | equivalência | holdout |
|---|---|---|---|---|---|---|---|---|---|
| [b_fn_saldo_cliente](b_fn_saldo_cliente/) | success | 1 | database_delegated / database_delegated | — | ok | 2109 / 477 | 52.9s | [3/3 (100%)](b_fn_saldo_cliente/evaluation.json) | 1/1 |
| [c_sp_atualizar_status_contas_inativas](c_sp_atualizar_status_contas_inativas/) | success | 2 | hybrid / hybrid | DIAGNOSTICS_SEMANTICS | ok | 3238 / 885 | 14.4s | [4/4 (100%)](c_sp_atualizar_status_contas_inativas/evaluation.json) | 2/2 |
| [d_sp_transferir_entre_contas](d_sp_transferir_entre_contas/) | success | 2 | hybrid / hybrid | ROW_LOCKING | ok | 5397 / 2432 | 104.7s | [7/7 (100%)](d_sp_transferir_entre_contas/evaluation.json) | 3/3 |
| [e_sp_processar_lote_taxas](e_sp_processar_lote_taxas/) | success | 1 | hybrid / hybrid | N_PLUS_ONE | ok | 3914 / 3612 | 43.0s | [3/3 (100%)](e_sp_processar_lote_taxas/evaluation.json) | 1/1 |
| [f_sp_relatorio_mensal_cliente](f_sp_relatorio_mensal_cliente/) | success | 1 | hybrid / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, RECURSIVE_CTE, SWALLOWED_EXCEPTION | ok | 4067 / 2994 | 65.5s | [4/4 (100%)](f_sp_relatorio_mensal_cliente/evaluation.json) | 2/2 |
