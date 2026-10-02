# Resultados — Anexos B a F

Modelo: `openrouter/z-ai/glm-5.3-flash` · schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.

Prompt: `generation-v5`. Equivalência: **5/5 rotinas (100%)**; **21/21 casos (100.0%)**. Só holdout (casos nunca mostrados ao LLM): **5/5 rotinas, 9/9 casos (100.0%)**. Validade estática (AST): 100%; conclusão: 100%.

Casos gerados pelo LLM (somados aos dev no pipeline): **ligado**.

| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff | tokens in/out (última) | duração total | casos gerados (mantidos / descartados) | equivalência | holdout |
|---|---|---|---|---|---|---|---|---|---|---|
| [b_fn_saldo_cliente](b_fn_saldo_cliente/) | success | 1 | database_delegated / database_delegated | — | ok | 2237 / 483 | 11.9s | 4 / 0 | [3/3 (100%)](b_fn_saldo_cliente/evaluation.json) | 1/1 |
| [c_sp_atualizar_status_contas_inativas](c_sp_atualizar_status_contas_inativas/) | success | 1 | hybrid / hybrid | DIAGNOSTICS_SEMANTICS | ok | 2832 / 1132 | 13.3s | 6 / 0 | [4/4 (100%)](c_sp_atualizar_status_contas_inativas/evaluation.json) | 2/2 |
| [d_sp_transferir_entre_contas](d_sp_transferir_entre_contas/) | success | 1 | hybrid / hybrid | ROW_LOCKING | ok | 3783 / 2313 | 31.5s | 6 / 0 | [7/7 (100%)](d_sp_transferir_entre_contas/evaluation.json) | 3/3 |
| [e_sp_processar_lote_taxas](e_sp_processar_lote_taxas/) | success | 2 | hybrid / hybrid | N_PLUS_ONE | ok | 5863 / 2348 | 72.7s | 6 / 0 | [3/3 (100%)](e_sp_processar_lote_taxas/evaluation.json) | 1/1 |
| [f_sp_relatorio_mensal_cliente](f_sp_relatorio_mensal_cliente/) | success | 2 | hybrid / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, RECURSIVE_CTE, SWALLOWED_EXCEPTION | ok | 6335 / 3319 | 125.0s | 6 / 0 | [4/4 (100%)](f_sp_relatorio_mensal_cliente/evaluation.json) | 2/2 |
