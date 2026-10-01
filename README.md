# PL/pgSQL Modernizer

Pipeline híbrido (LLM + regras) que moderniza stored procedures **PL/pgSQL** para **Python 3.14**,
orquestrado com **LangGraph** e exposto pelo servidor do **LangGraph CLI**:

```
PL/pgSQL → parsing (pglast) → análise semântica → geração (LLM) → validação → Python 3.14 + relatório
                                                       ▲                │
                                                       └── reparo ──────┘
```

As etapas determinísticas dão contexto e garantias à etapa não determinística: o prompt é montado a
partir do parsing e da análise (não da procedure bruta), e a validação checa o código gerado
estaticamente (AST, Ruff) e por comportamento (executa original e gerado num banco descartável).
Toda execução é gravada em `modernization_history`, qualquer que seja o desfecho.

**Resultado atual sobre os Anexos B–F:** 5/5 rotinas equivalentes, 21/21 casos; nos casos *holdout*
(nunca mostrados ao LLM), 9/9. Linha de base sem a validação por comportamento: 1/5 rotinas, 8/19
casos. Detalhes em [Métrica de evaluation](#métrica-de-evaluation).

## Sumário

- [Como rodar](#como-rodar)
- [Pipeline e grafo LangGraph](#pipeline-e-grafo-langgraph)
- [Arquitetura](#arquitetura)
- [Banco de dados](#banco-de-dados)
- [Endpoints e relatório](#endpoints-e-relatório)
- [Métrica de evaluation](#métrica-de-evaluation)
- [Observabilidade com Langfuse](#observabilidade-com-langfuse)
- [Testes e qualidade](#testes-e-qualidade)
- [Decisões técnicas e trade-offs](#decisões-técnicas-e-trade-offs)
- [Escalabilidade](#escalabilidade)
- [Configuração](#configuração)
- [Limitações e evolução](#limitações-e-evolução)

---

## Como rodar

### Docker Compose

Copie `.env.example` para `.env` e preencha `LLM_API_KEY` (o modelo usado nos resultados é
`z-ai/glm-5.3-flash` via OpenRouter, com `LLM_REASONING_EFFORT=low`).

```bash
docker compose up --build
```


| serviço    | papel                                                                                 |
| ---------- | ------------------------------------------------------------------------------------- |
| `postgres` | PostgreSQL 17; o init cria também `modernizer_test` e `modernizer_eval`               |
| `migrate`  | `alembic upgrade head`, uma vez, depois que o Postgres fica healthy                   |
| `app`      | `langgraph dev` em `:8000`: grafo, API nativa do LangGraph, Studio e as rotas FastAPI |


```bash
curl localhost:8000/health
```

```bash
curl -X POST localhost:8000/modernize -H "content-type: application/json" -d '{"source_code": "CREATE FUNCTION add_one(p int) RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN p + 1; END $$;"}'
```

O mesmo grafo pela API nativa do LangGraph (também gravado em `modernization_history`):

```bash
curl -X POST localhost:8000/runs/wait -H "content-type: application/json" -d '{"assistant_id": "modernization", "input": {"source_code": "CREATE FUNCTION add_one(p int) RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN p + 1; END $$;"}}'
```

Studio: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:8000`. OpenAPI:
`http://localhost:8000/docs`. Requisições prontas (os cinco anexos com schema, erros, API do
LangGraph) em `[docs/api.http](docs/api.http)`.

> Windows: se `localhost` não responder, use `127.0.0.1` (o relay do WSL pode ocupar `[::1]:5432`).

### Local (uv)

```bash
uv sync
```

```bash
docker compose up -d postgres
```

```bash
uv run alembic upgrade head
```

```bash
uv run langgraph dev --allow-blocking
```

`--allow-blocking`: o `langgraph dev` acusa I/O síncrono no event loop, e o `asyncpg` lê
`~/.postgresql` ao abrir conexões (microssegundos, interno do driver).

### Anexos B–F

`examples/schema.sql` (Anexo A) e `examples/procedures/*.sql` (Anexos B–F) são o material do
desafio. O script roda os cinco pelo mesmo caso de uso do `POST /modernize` (cada execução é
persistida), avalia cada uma e grava os resultados:

```bash
uv run python -m scripts.run_examples
```

Saída em `examples/results/`: `<anexo>/generated.py`, `report.json`, `evaluation.json` e
`[SUMMARY.md](examples/results/SUMMARY.md)`. Os arquivos versionados são uma rodada real, sem
curadoria: falhas ficam como saíram.

---

## Pipeline e grafo LangGraph

```mermaid
flowchart LR
    START((START)) --> record_start
    record_start --> parsing
    parsing --> semantic_analysis
    semantic_analysis --> generation
    generation --> validation
    validation -- "reprovado, tentativas e tempo sobrando" --> generation
    validation -- "aprovado ou sem retry" --> record_result
    record_result --> END((END))
```




| nó                  | o que faz                                                                                                               | implementação           |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| `record_start`      | grava a execução como `running` antes do LLM                                                                            | `ExecutionLog.start`    |
| `parsing`           | PL/pgSQL → IR `ParsedProcedure` (parâmetros, declarações, árvore de statements, SQL embutido com tabelas/funções/locks) | `PglastParser`          |
| `semantic_analysis` | features, riscos (N+1, `FOR UPDATE`, exceção engolida, SQL dinâmico…), dependências e estratégia recomendada            | `SemanticAnalyzer`      |
| `generation`        | prompt montado a partir das duas etapas anteriores → LLM → contrato JSON                                                | `GenerateCode`          |
| `validation`        | `ast.parse` (bloqueante), Ruff e comportamento contra a rotina original (não bloqueantes)                               | `ValidateCode`          |
| `record_result`     | calcula o status e grava código e relatório                                                                             | `ExecutionLog.complete` |


- **Estado tipado** (`graph/state.py`): `ModernizationState` é um `TypedDict` com os modelos de
domínio de cada etapa e três canais append-only (`completed_steps`, `warnings`, `errors`).
- **Nós finos** (`graph/nodes.py`): leem o estado, chamam um colaborador e devolvem um
`StateUpdate`. Nenhuma regra de negócio mora no grafo.
- **Falha em qualquer etapa**: um wrapper em volta de cada nó grava `failure` com tudo o que já foi
produzido e relança a exceção, que vira resposta HTTP no handler global. Funciona igual pelo
`POST /modernize`, pela API do LangGraph e pelo Studio, porque a gravação está no próprio grafo.
- **Loop de reparo** (`validation → generation`): se algum check reprova, a geração roda de novo
com o código anterior e a lista de problemas no prompt. Limites: `GENERATION_MAX_ATTEMPTS`
(padrão 2) e `GENERATION_RETRY_BUDGET_SECONDS` (padrão 90 s), porque a request é síncrona.

---

## Arquitetura

Package by feature e, dentro da feature, uma pasta por capacidade. Sem `__init__.py` (namespace
packages).

```
app/
├── main.py                     # FastAPI app (referenciada pelo langgraph.json)
├── core/                       # composition root, config, banco, handlers HTTP
│   ├── providers.py            # dishka: único lugar que conhece as implementações
│   ├── bootstrap.py            # container por processo + make_graph() do langgraph.json
│   ├── server.py · exception_handlers.py
│   ├── config/settings.py      # pydantic-settings: única fonte de configuração
│   └── database/               # engine, session factory, Base declarativa
├── shared/
│   ├── errors.py               # AppError · DomainError · NotFoundError
│   ├── resilience/             # CircuitBreaker
│   └── integrations/           # IntegrationError, Integration (retries + breaker), llm/
│       └── llm/                # port LLM, LLMGateway (rotas com failover), providers, TracedLLM
└── features/
    ├── health/
    └── modernization/
        ├── routes.py · schemas.py · use_cases.py
        ├── domain/             # IR, análise, relatório, aggregate Modernization (Pydantic)
        ├── parsing/            # SQLParser (strategy) · PglastParser
        ├── generation/         # GenerateCode · prompt
        ├── validation/         # ValidateCode + Rule · checks: AST, Ruff, comportamento
        ├── evaluation/         # harness de equivalência, dataset, runner, repositório
        ├── persistence/        # repositório, ExecutionLog, model ORM, mapper
        └── graph/              # builder · nodes · state
migrations/  scripts/run_examples.py  examples/  tests/{unit,integration}  docker/
```

**Fluxo de uma request:** rota → caso de uso → grafo → nós → steps (`GenerateCode`,
`ValidateCode`) e strategies (`SQLParser`, `CodeCheck`). Persistência e LLM entram por injeção.

**Abstração só onde há variação real:**


| eixo              | contrato                                          | implementações                                        | nos testes   |
| ----------------- | ------------------------------------------------- | ----------------------------------------------------- | ------------ |
| LLM               | `LLM` (port)                                      | `LLMGateway` → `OpenRouterProvider`, `OpenAIProvider` | `FakeLLM`    |
| Persistência      | `ModernizationRepository`, `EvaluationRepository` | SQLAlchemy                                            | em memória   |
| Dialeto de origem | `SQLParser` (strategy)                            | `PglastParser`                                        | parser real  |
| Checks do código  | `CodeCheck` + `Rule(check, blocking)`             | AST, Ruff, comportamento                              | checks reais |
| Métrica           | `EquivalenceMetric`                               | `BehavioralEquivalence`                               | `FakeMetric` |


O grafo não fica atrás de interface: só existe um orquestrador, e os testes rodam o grafo real com
fakes nas bordas (LLM e banco). `tests/unit/test_architecture.py` fixa as regras de dependência:
cada biblioteca só é importada pelo módulo que a encapsula (`pglast` em `parsing/plpgsql.py`, `ruff`
em `validation/ruff_check.py`, `langgraph` em `graph/builder.py`), o domínio não importa
infraestrutura e as features não importam o composition root.

---

## Banco de dados

Schema criado só por Alembic (`migrations/versions/`, `env.py` async lendo a URL do `Settings`):


| tabela                  | colunas                                                                                                                                                                                              |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `modernization_history` | `id` UUIDv7, `source_code`, `schema_context`, `generated_code`, `report` JSONB, `status` (CHECK: `running`/`success`/`partial`/`failure`), `created_at`, `updated_at`; índice `(status, created_at)` |
| `evaluation_results`    | FK para a execução, rotina, métrica, prompt/modelo, validade estática, conclusão, casos aprovados/totais, score, casos em JSONB, data; índice `(procedure_name, created_at)`                         |


**Toda execução é persistida.** O `ExecutionLog` grava `running` no início e o desfecho no fim,
cada um com commit próprio: a linha existe antes da chamada ao LLM (se o processo morrer, a
execução fica visível) e nenhuma conexão fica presa durante a geração. Os repositórios recebem a
session factory e abrem uma sessão por operação, então requests concorrentes nunca compartilham
sessão.

O banco `modernizer_eval` só recebe schemas descartáveis da avaliação. Em volumes criados antes
dele existir:

```bash
docker compose exec postgres psql -U modernizer -d postgres -c "CREATE DATABASE modernizer_eval OWNER modernizer"
```

---

## Endpoints e relatório


| método | rota                              | descrição                                             |
| ------ | --------------------------------- | ----------------------------------------------------- |
| GET    | `/health`                         | `{"status": "ok"}`                                    |
| POST   | `/modernize`                      | roda o pipeline e devolve código + relatório          |
| GET    | `/modernizations/{id}`            | uma execução gravada (404 se não existe)              |
| POST   | `/modernizations/{id}/evaluation` | roda a métrica sobre uma execução e grava o resultado |
| GET    | `/evaluations`                    | última avaliação de cada rotina e as taxas agregadas  |


```json
{ "source_code": "CREATE OR REPLACE FUNCTION ...", "schema": "CREATE TABLE contas (...);" }
```

`schema` é opcional. A resposta traz `execution_id`, `status`, `generated_code` e `report`:


| seção               | conteúdo                                                                                                                    |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `parsing`           | nome, tipo, parâmetros e modos, retorno, nº de statements, tabelas, funções chamadas                                        |
| `semantic_analysis` | features (com linhas), riscos (`code`, `severity`, `message`, `line`), dependências, estratégia recomendada                 |
| `generation`        | estratégia escolhida e recomendada, decisões arquiteturais, provider, modelo, `prompt_version`, tokens, latência, tentativa |
| `validation`        | resultado por check (`validator`, `success`, `blocking`, `messages`, `skipped`)                                             |
| raiz                | `completed_steps` (repete geração/validação a cada tentativa), `errors`, `warnings`                                         |



| status    | quando                                                                          |
| --------- | ------------------------------------------------------------------------------- |
| `running` | gravado antes do LLM                                                            |
| `success` | código gerado e todos os checks passaram                                        |
| `partial` | Python válido, mas Ruff acusou algo ou o comportamento divergiu em algum caso   |
| `failure` | exceção numa etapa (progresso preservado) ou Python inválido depois dos reparos |


A regra fica em `ModernizationReport.status()` (domínio). Erros viram HTTP pela classe:
`DomainError` → 400 (SQL inválido, linguagem não suportada), `NotFoundError` → 404,
`IntegrationError` → 502, 503 com `Retry-After` (circuito aberto) ou 504 (timeout), qualquer outra
→ 500. O corpo traz o `execution_id`, e a execução já está gravada como `failure`.

---

## Métrica de evaluation

**Métrica: equivalência comportamental.** Para cada caso do dataset
(`[examples/evaluation/scenarios.yml](examples/evaluation/scenarios.yml)`), o harness cria um
schema descartável em `modernizer_eval` com o Anexo A, um seed e a rotina original; executa a
original (SQL) e o módulo gerado (Python, num subprocesso), cada um numa transação revertida; e
compara o resultado e o estado final das cinco tabelas. Se os dois lados lançam erro, o Python
precisa lançar uma exceção definida no próprio módulo (erro de driver ou de import não conta).
Números são comparados como `Decimal`.

Taxa principal: rotinas com todos os casos aprovados / rotinas avaliadas. Taxa por caso, validade
estática (AST) e conclusão (terminou com código) são publicadas à parte. Resultados em
`evaluation_results` e no `GET /evaluations`.

**Por que essa métrica.** AST e Ruff aprovaram 5/5 módulos na primeira versão do prompt, e só 1/5
se comportava como o original. Os defeitos reais eram de execução: bind sem tipo que o asyncpg
rejeita, `begin_nested()` sem `await`, arredondamento `NUMERIC` em cada atribuição,
`UPDATE … FROM` aplicando uma linha só, fallback rodando numa transação abortada. Só executando
contra o original isso aparece.

**Dentro do pipeline, com holdout.** O mesmo harness roda como check de validação
(`validation/behavior_check.py`): uma divergência vira feedback para o loop de reparo. Para a
métrica continuar medindo generalização, o dataset separa **12 casos dev** (rodam no pipeline e
viram feedback) de **9 holdout** (`holdout: true`, nunca chegam ao prompt). Cada rotina tem os dois
tipos, e o holdout repete as armadilhas com outros dados. Rotina sem cenário no dataset: o check
responde "não verificado" no relatório, e o status não finge verificação.

**O que fica de fora:** só as entradas do dataset (não é prova geral); NOTICE/logs, concorrência,
custo das queries e mensagens exatas de erro não são comparados; `id`, `data_transacao` e
`criado_em` são ignorados para tolerar sequências e relógio. O subprocesso isola crash e travamento
do servidor, mas não é um sandbox (mesma máquina, com rede).

**Em produção:** gerar o dataset automaticamente para qualquer rotina (ver
[Evolução](#limitações-e-evolução)), comparar SQLSTATE e estado após falhas, medir cobertura dos
ramos do PL/pgSQL, executar o código gerado em container sem rede e com papel de banco restrito.

### Rodadas

Mesmo modelo, Anexo A como schema, temperatura 0. A [linha de base v2](examples/evaluation/baseline-v2.json)
é anterior ao prompt com armadilhas de runtime e ao check de comportamento.


| rodada                             | pipeline                             | rotinas       | casos            | holdout | o que falhou ou custou tentativa                          |
| ---------------------------------- | ------------------------------------ | ------------- | ---------------- | ------- | --------------------------------------------------------- |
| v2                                 | AST + Ruff                           | 1/5           | 8/19             | —       | C, D, E, F                                                |
| v3 #1–#3                           | AST + Ruff                           | 4/5, 5/5, 4/5 | 14, 19, 16 de 19 | —       | D: bind sem tipo; F: `INTERVAL` como `str`                |
| v3 #4                              | + comportamento no loop              | 4/5           | 14/21            | 6/9     | D: retentativa voltou com cerca Markdown                  |
| v3 #5                              | idem                                 | 5/5           | 21/21            | 9/9     | D e F corrigidos na 2ª tentativa                          |
| v4 #1                              | + cerca removida, contrato tolerante | 5/5           | 21/21            | 9/9     | F corrigido na 2ª tentativa                               |
| v4 #2                              | idem                                 | 4/5           | 20/21            | 8/9     | D: falha num caso holdout                                 |
| **v4 #3** (em `examples/results/`) | idem                                 | **5/5**       | **21/21**        | **9/9** | C e D: import não usado (Ruff), corrigido na 2ª tentativa |


A mesma versão do pipeline oscila entre rodadas (temperatura 0 não torna o provider
determinístico); por isso todas ficam registradas. A v4 #2 mostra para que serve o holdout. O D passou nos quatro casos dev, que o loop de reparo
viu, e falhou no holdout "origem inexistente". O código busca a conta com `.one()`, que lança
`NoResultFound` quando não há linha; o original faz `SELECT … INTO`, recebe `NULL` e lança a
própria exceção. Sem o holdout, a rodada contaria como 5/5.

### Decisões de tradução por anexo (rodada v4 #3)


| anexo                                   | estratégia           | tentativas | casos (dev · holdout) |
| --------------------------------------- | -------------------- | ---------- | --------------------- |
| B `fn_saldo_cliente`                    | `database_delegated` | 1          | 2/2 · 1/1             |
| C `sp_atualizar_status_contas_inativas` | `hybrid`             | 2          | 2/2 · 2/2             |
| D `sp_transferir_entre_contas`          | `hybrid`             | 2          | 4/4 · 3/3             |
| E `sp_processar_lote_taxas`             | `hybrid`             | 1          | 2/2 · 1/1             |
| F `sp_relatorio_mensal_cliente`         | `hybrid`             | 1          | 2/2 · 2/2             |


- **B:** uma consulta agregada, toda em SQL, com `CAST(… AS NUMERIC(18,2))` e retorno `Decimal`.
O módulo declara uma exceção que não usa (o original não tem `RAISE`); Ruff não acusa classe
não usada.
- **C:** valida `p_dias` em Python com exceção própria; o `UPDATE … NOT EXISTS` fica no banco;
`GET DIAGNOSTICS` vira `result.rowcount`; o OUT vira uma dataclass. A 1ª tentativa tinha um
import não usado (Ruff).
- **D:** o corpo inteiro roda num savepoint (`async with conn.begin_nested()`), porque o
`EXCEPTION WHEN OTHERS` original cobre também os `RAISE` de validação. `FOR UPDATE` e escritas
na mesma conexão, valores `Decimal`, uma exceção por `RAISE`. As buscas usam `.first()` e tratam
a conta ausente como o `SELECT … INTO` original (o defeito da v4 #2). No handler, grava a
auditoria de erro e relança; um erro que não é de validação sai embrulhado em
`TransferenciaError`, enquanto o original relança o erro em si. A métrica aceita, porque não
compara o tipo exato do erro (ver "O que fica de fora"). A 1ª tentativa tinha um import não
usado (Ruff).
- **E:** o loop cursor-a-cursor (N+1) virou **um único statement**: taxa vigente por
`LEFT JOIN LATERAL`, os dois arredondamentos `NUMERIC(18,2)` intermediários, débito agregado por
conta antes do `UPDATE … FROM` (duas tarifas na mesma conta) e os inserts de tarifa e de
auditoria como CTEs de escrita. O log do lote é um segundo statement com os totais. O
`logger.info` é um acréscimo do LLM (o original não tem NOTICE).
- **F:** CTE recursiva, agregações e a chamada a `fn_saldo_cliente` ficam em SQL. A validação de
período roda **dentro** do savepoint, como no original: o `WHEN OTHERS` captura o período
invertido e devolve a linha degradada, montada por uma consulta com aliases explícitos.

---

## Observabilidade com Langfuse

Langfuse v4 **self-hosted** pelo [Compose oficial](https://langfuse.com/self-hosting/deployment/docker-compose)
(`docker-compose.langfuse.yml`: web, worker, Postgres, ClickHouse, Redis, MinIO; portas só em
localhost). Escolha por reprodutibilidade: nenhum dado sai da máquina e não depende de conta.

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Preencha as variáveis `LANGFUSE_*` do `.env.example`; organização, projeto, chaves e o login
(`LANGFUSE_LOCAL_USER_*`) são provisionados na primeira subida. UI em `http://localhost:3000`.
Sem as chaves, o tracing fica desligado.

O que é registrado: um trace por execução, um span por nó do grafo (inclusive cada tentativa do
loop de reparo), via o callback nativo do LangChain/LangGraph; e a chamada ao LLM como geração
filha do nó `generation` (`TracedLLM`), com prompt, resposta, modelo, tokens e latência. Custo
aparece quando o modelo tem preço cadastrado no Langfuse.

![Trace do Anexo D no Langfuse](docs/langfuse/langfuse_screenshot_1.png)

---

## Testes e qualidade

```bash
uv run ruff check . && uv run ruff format --check .
```

```bash
uv run pytest
```

Com Postgres (`docker compose up -d postgres`), os testes de integração rodam também:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://modernizer:modernizer@127.0.0.1:5432/modernizer_test EVALUATION_DATABASE_URL=postgresql+asyncpg://modernizer:modernizer@127.0.0.1:5432/modernizer_eval uv run pytest --cov
```

- **232 testes** (222 unitários, 10 de integração); nenhum chama um LLM real.
- **Cobertura 96%** com integração (branches incluídos, subprocesso da avaliação medido);
`fail_under = 95`. Só com unitários fica em 91%, porque o harness precisa de Postgres.
- **Unitários:** parser e análise semântica (inclusive regressões medidas nos anexos), prompt e
contrato do LLM (cercas Markdown, resposta truncada, decisões fora do contrato), checks e
política de bloqueio, loop de reparo (feedback, limite de tentativas, orçamento de tempo),
persistência de sucesso e falha, API, gateway de LLM (failover, circuit breaker) e regras de
arquitetura.
- **Integração:** Postgres migrado por Alembic, repositórios, JSONB, equivalência contra o
PL/pgSQL real e o ciclo completo do loop de reparo com o harness (um B errado é corrigido pelo
feedback dos casos dev e nenhum nome de caso holdout aparece no prompt).

---

## Decisões técnicas e trade-offs

### 1. Parsing com pglast, sem regex

`pglast` usa o parser do próprio PostgreSQL (libpg_query), então lê PL/pgSQL e SQL exatamente como
o banco. `sqlglot` e `sqlparse` não entendem o corpo PL/pgSQL. `parse_plpgsql` dá a árvore de
controle (blocos, loops, IF, RAISE, EXCEPTION, cursores); o cabeçalho vem de `parse_sql`; o SQL
embutido é re-parseado e percorrido com um `Visitor` (tabelas, funções, CTE, `FOR UPDATE`, JSONB).
O resultado é um IR próprio (`ParsedProcedure`): objetos do pglast nunca saem do adapter, e um
dialeto novo é outra implementação de `SQLParser` produzindo o mesmo IR.

### 2. Análise semântica determinística e a escolha SQL × Python

`SemanticAnalyzer` é domínio puro sobre o IR: detecta features e riscos e recomenda a estratégia.


| condição                  | estratégia                |
| ------------------------- | ------------------------- |
| sem SQL embutido          | `python_reimplementation` |
| só SQL set-based          | `database_delegated`      |
| SQL + controle procedural | `hybrid`                  |


Princípio: **lógica relacional fica perto do banco**. Joins, agregações, CTEs, DML em massa e locks
continuam como SQL parametrizado (`sqlalchemy.text()` com binds); Python coordena validação, fluxo,
erros e composição; a transação é do chamador. Reescrever em Python puro mudaria semântica
(arredondamento, NULL, locks) e desempenho sem ganho. O LLM recebe a recomendação e pode divergir
justificando; o relatório guarda as duas.

### 3. Prompt a partir da análise, contrato tolerante

O prompt leva assinatura, declarações, um outline do fluxo de controle com linhas, o SQL embutido
com fatos extraídos, riscos e estratégia, o schema e, por último, o source original como
referência. As regras incluem as armadilhas de runtime medidas (binds com `CAST`, savepoints com
`async with`, `Decimal` e arredondamento por atribuição, agregação antes de `UPDATE … FROM`).
A resposta é JSON validado com Pydantic. Desvios que não afetam o código são tolerados com warning
(cerca Markdown dentro do código, decisão arquitetural malformada), porque cada um custava uma
tentativa ou abortava a execução. `PROMPT_VERSION` vai para o relatório.

### 4. LLM atrás de um gateway

As features dependem só do port `LLM`. O `LLMGateway` tenta rotas (provider + modelo) em ordem:
uma rota que falha com `IntegrationError` passa a vez para a próxima, dentro de um orçamento de
tempo. Cada provider tem retries com backoff e um circuit breaker; só falhas transitórias (timeout,
transporte, 408/429/5xx) contam. Trocar de modelo é configuração (`LLM_MODEL`, ou rotas em YAML via
`LLM_CONFIG_FILE`); um provider novo é uma classe e uma entrada no registry. OpenRouter dá acesso a
muitos modelos com uma chave, ao custo de um hop a mais.

Modelos de raciocínio contam os tokens de "pensamento" no limite de saída: com `glm-5.3-flash` sem
limite de esforço, o Anexo F gastou mais de 32k tokens pensando e voltou vazio; com
`LLM_REASONING_EFFORT=low`, ~2k tokens e ~11 s.

### 5. Validação: checks reportam, regras decidem

Cada `CodeCheck` só devolve achados; se o achado bloqueia (`failure`) ou rebaixa para `partial` é
política, declarada uma vez em `core/providers.py`: AST bloqueante, Ruff e comportamento não
bloqueantes. Os checks rodam em paralelo. Ruff roda como subprocesso numa worker thread (o event
loop do `langgraph dev` no Windows não suporta subprocess async), com regras de correção e `S608`
(SQL montado com f-string). Divergência de comportamento não bloqueia porque o código continua sendo
Python válido: o status `partial` e o relatório dizem qual caso diverge.

### 6. Persistência no grafo

A gravação fica nos nós `record_start`/`record_result` e no wrapper de falha, e não no caso de uso,
porque o grafo tem três portas de entrada (`POST /modernize`, API do LangGraph, Studio) e todas
precisam gravar. Uma execução que não consegue ser gravada falha alto.

### 7. Composition root com dishka

Cada dependência é declarada uma vez em `core/providers.py`; o container valida o grafo de
dependências no boot (chave de LLM ausente derruba o startup, não a primeira request) e fecha
recursos no shutdown. O `langgraph dev` serve a API FastAPI e a factory `make_graph()` no mesmo
processo; as duas usam o mesmo container, logo um engine e um grafo por processo. Testes
sobrescrevem só os tipos que precisam (LLM, repositórios, métrica).

### 8. Erros por papel, tratados num lugar

Três classes de erro da aplicação mais `IntegrationError`: a classe diz quem é o culpado e o
handler global deriva o status; a mensagem e o payload são públicos (texto de SDK fica no
`__cause__`, que é logado). `try/except` local só onde o resultado precisa ser observado ali (lista
fixada em `test_architecture.py`).

### 9. `langgraph dev` no container

O desafio pede o servidor do LangGraph CLI. O `langgraph dev` serve grafo, API nativa, Studio e as
rotas FastAPI (via `http.app`) num processo, sem dependências externas. `langgraph up`/`build`
exige Redis, Postgres do runtime e licença; ficou como caminho de produção. Migrations rodam num
serviço one-shot antes da app.


| escolha                   | ganho                                        | custo                                         |
| ------------------------- | -------------------------------------------- | --------------------------------------------- |
| LLM gera, regras checam   | traduz o que regras não cobririam            | não determinismo; exige validação e relatório |
| Validação por execução    | pega os defeitos que AST/Ruff não veem       | precisa de dataset e de um banco descartável  |
| Holdout fora do loop      | a métrica continua medindo generalização     | menos feedback para o reparo                  |
| Dois commits por execução | rastreabilidade mesmo se o processo morre    | estado `running` intermediário visível        |
| Pydantic no domínio       | serialização para JSONB e validação de graça | dependência de lib no núcleo                  |
| API síncrona              | simples de usar e testar                     | request longa (ver Escalabilidade)            |


---

## Escalabilidade

**Gargalo medido:** parsing e análise levam milissegundos; a geração leva de 7 s a 100 s por
procedure. Escalar o pipeline é escalar **chamadas concorrentes ao LLM** dentro do rate limit do
provider; CPU e banco não são o limite.

**Já no código:** processo sem estado (tudo no Postgres), então N réplicas atrás de um balanceador
funcionam sem coordenação; I/O async de ponta a ponta; um pool de conexões por processo e nenhuma
conexão presa durante o LLM; checks em paralelo; execuções independentes concorrentes
(`run_examples` dispara as cinco com `asyncio.gather`); failover e circuit breaker no LLM.

**Filas (próximo passo):** `POST /modernize` responde `202` com o `execution_id` (a linha `running`
já é gravada antes do LLM) e o cliente consulta `GET /modernizations/{id}`, que já existe. O
trabalho vai para uma fila: a do próprio runtime do LangGraph em produção (`langgraph build`, Redis +
Postgres), ou SQS/RabbitMQ com workers chamando o mesmo caso de uso. Um semáforo por provider,
dimensionado pelo rate limit, troca `429` por espera na fila.

**Cache:** chave `sha256(source normalizado + schema + modelo + PROMPT_VERSION + regras)` numa coluna
indexada; submissão repetida devolve o resultado `success` sem chamar o LLM (a execução continua
sendo gravada). O system prompt é estático e vem primeiro, então serve de prefixo para o prompt
caching do provider. Parsing e análise não valem cache.

**Novos dialetos e modelos:** um `SQLParser` por dialeto sobre o mesmo IR; um modelo novo é
configuração; um check novo é um `CodeCheck` e uma `Rule`.

**Banco em volume:** particionar `modernization_history` por mês, retenção para o `report` e leitura
por réplica.

---

## Configuração

`app/core/config/settings.py` (pydantic-settings, lê env e `.env`). Principais variáveis:


| variável                                                            | padrão                                                                 | descrição                                                                 |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `DATABASE_URL`                                                      | `postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer` | driver async                                                              |
| `LLM_PROVIDER` / `LLM_MODEL`                                        | `openrouter` / `anthropic/claude-sonnet-4.5`                           | uma rota; `openai` aceita `LLM_BASE_URL`                                  |
| `LLM_API_KEY`                                                       | —                                                                      | obrigatória (o boot falha sem ela)                                        |
| `LLM_REASONING_EFFORT`                                              | não enviado                                                            | `low` para modelos de raciocínio (decisão 4)                              |
| `LLM_CONFIG_FILE`                                                   | —                                                                      | YAML com várias rotas e providers ([exemplo](config/llm.example.yml))     |
| `LLM_TEMPERATURE` / `LLM_MAX_OUTPUT_TOKENS`                         | `0` / `8192`                                                           |                                                                           |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES`                           | `120` / `2`                                                            |                                                                           |
| `LLM_CIRCUIT_BREAKER_*`                                             | `5` falhas / `60` s                                                    | abre e fecha o circuito                                                   |
| `GENERATION_MAX_ATTEMPTS`                                           | `2`                                                                    | tentativas de geração (1 desliga o reparo)                                |
| `GENERATION_RETRY_BUDGET_SECONDS`                                   | `90`                                                                   | nenhuma retentativa começa depois disso                                   |
| `EVALUATION_DATABASE_URL`                                           | —                                                                      | banco descartável da avaliação; sem ele o check de comportamento é pulado |
| `EVALUATION_DATASET_FILE`                                           | `examples/evaluation/scenarios.yml`                                    |                                                                           |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | — / — / `http://localhost:3000`                                        | as duas chaves ligam o tracing                                            |
| `TEST_DATABASE_URL`                                                 | —                                                                      | só testes de integração                                                   |


---

## Limitações e evolução

**Limitações:**

- Só `LANGUAGE plpgsql`; o primeiro `CREATE FUNCTION/PROCEDURE` do arquivo.
- Sem catálogo: `%TYPE`/`%ROWTYPE` não são resolvidos (viram placeholder com warning), e builtin ×
rotina do usuário é heurística.
- `EXECUTE` (SQL dinâmico) não é analisável: vira risco `DYNAMIC_SQL`.
- Comportamento só é verificado onde há dataset (B–F). Para qualquer outra rotina a validação é
estática e o relatório diz isso.
- `POST /modernize` é síncrono.

**Com mais tempo:**

- **Dataset de avaliação gerado automaticamente**, como um nó antes da geração: banco de teste a
partir do `schema_context`; seed e entradas derivados do IR (tipos, colunas, constantes de
`WHERE`/`IF` como valores de borda), por LLM ou fuzzing; a rotina original como oráculo (casos que
não exercitam nada são descartados); a mesma divisão dev/holdout. Riscos: cobertura de ramos
(medir com `plpgsql_check`), viés de usar o mesmo LLM para código e dados, rotinas com efeitos
externos.
- Modo assíncrono com fila, cache de resultado e backpressure por provider.
- Executar o código gerado em container sem rede e com papel de banco restrito.
- Resolver `%TYPE` e builtins consultando o catálogo quando houver conexão.
- Novos dialetos (T-SQL, PL/SQL) implementando `SQLParser`.
- Comparar modelos e prompts por rodada com a mesma métrica; providers nativos (Anthropic, Gemini).

