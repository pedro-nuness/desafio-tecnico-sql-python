# Resultados — Anexos B a F

Modelo: `openrouter/z-ai/glm-5.3-flash` · schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.

| procedure | status | estratégia (LLM / recomendada) | riscos | ruff | tokens in/out | latência |
|---|---|---|---|---|---|---|
| [b_fn_saldo_cliente](b_fn_saldo_cliente/) | success | database_delegated / database_delegated | — | ok | 1552 / 560 | 4.1s |
| [c_sp_atualizar_status_contas_inativas](c_sp_atualizar_status_contas_inativas/) | failure | hybrid / hybrid | DIAGNOSTICS_SEMANTICS | 1 | 2147 / 1112 | 6.5s |
| [d_sp_transferir_entre_contas](d_sp_transferir_entre_contas/) | partial | hybrid / hybrid | ROW_LOCKING | 1 | 3098 / 2578 | 13.0s |
| [e_sp_processar_lote_taxas](e_sp_processar_lote_taxas/) | success | database_delegated / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, N_PLUS_ONE | ok | 3461 / 3100 | 16.7s |
| [f_sp_relatorio_mensal_cliente](f_sp_relatorio_mensal_cliente/) | partial | hybrid / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, RECURSIVE_CTE | 1 | 3483 / 2058 | 136.2s |
