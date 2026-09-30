# Resultados — Anexos B a F

Modelo: `openrouter/z-ai/glm-5.3-flash` · schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.

| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff | tokens in/out (última) | duração total |
|---|---|---|---|---|---|---|---|
| [b_fn_saldo_cliente](b_fn_saldo_cliente/) | success | 1 | database_delegated / database_delegated | — | ok | 1552 / 647 | 4.7s |
| [c_sp_atualizar_status_contas_inativas](c_sp_atualizar_status_contas_inativas/) | success | 2 | hybrid / hybrid | DIAGNOSTICS_SEMANTICS | ok | 2762 / 1002 | 10.8s |
| [d_sp_transferir_entre_contas](d_sp_transferir_entre_contas/) | success | 1 | hybrid / hybrid | ROW_LOCKING | ok | 3098 / 1978 | 10.3s |
| [e_sp_processar_lote_taxas](e_sp_processar_lote_taxas/) | success | 1 | database_delegated / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, N_PLUS_ONE | ok | 3461 / 3357 | 15.5s |
| [f_sp_relatorio_mensal_cliente](f_sp_relatorio_mensal_cliente/) | success | 1 | hybrid / hybrid | EXTERNAL_ROUTINE_DEPENDENCY, RECURSIVE_CTE | ok | 3483 / 2092 | 110.0s |
