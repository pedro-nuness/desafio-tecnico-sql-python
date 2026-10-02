# Historico de Execucoes e Tracking de Metricas

Rastreamento consolidado das execucoes do pipeline (`scripts/run_examples.py`).
Acompanha a evolucao das taxas de equivalencia comportamental, holdout, tempo e tokens.

| Run ID | Data (UTC) | Tag | Modelo | Prompt | Casos LLM | Equivalencia | Casos (Geral) | Holdout | Tokens (In / Out) | Duracao |
|---|---|---|---|---|---|---|---|---|---|---|
| `20261002_034927` | 2026-10-02 03:49 | `sem-source-2` | `z-ai/glm-5.3-flash` | `code-generation-v5-sem-source` | Ligado | **4/5 (80%)** | 19/21 (90.5%) | 8/9 (88.9%) | 22.2k / 13.0k | 331.0s |
| `20261002_034351` | 2026-10-02 03:43 | `sem-source-1` | `z-ai/glm-5.3-flash` | `code-generation-v5-sem-source` | Ligado | **5/5 (100%)** | 21/21 (100.0%) | 9/9 (100.0%) | 19.7k / 15.1k | 376.1s |
