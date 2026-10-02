# PL/pgSQL Modernizer

Converte uma function/procedure PL/pgSQL em um módulo **Python 3.14** async (SQLAlchemy) por
um grafo LangGraph: parsing com o parser do próprio PostgreSQL, análise semântica determinística,
geração por LLM e validação **executando o código gerado contra a rotina original**. Toda
execução fica gravada no Postgres com o código e um relatório técnico.

Nos Anexos B–F: **5/5 rotinas equivalentes, 21/21 casos, 9/9 casos holdout** (nunca mostrados ao
LLM), em duas rodadas seguidas ([histórico](examples/results/HISTORY.md)).

## Sumário

- [Como rodar](#como-rodar)
- [Pipeline](#pipeline)
- [Arquitetura](#arquitetura)
- [Banco de dados](#banco-de-dados)
- [API e relatório](#api-e-relatório)
- [Métrica de evaluation](#métrica-de-evaluation)
- [Observabilidade com Langfuse](#observabilidade-com-langfuse)
- [Testes e qualidade](#testes-e-qualidade)
- [Decisões técnicas](#decisões-técnicas)
- [Escalabilidade](#escalabilidade)
- [Configuração](#configuração)
- [Limitações e evolução](#limitações-e-evolução)

---

## Como rodar

### Docker Compose

Copie `.env.example` para `.env` e preencha `LLM_API_KEY` (os resultados usam
`z-ai/glm-5.3-flash` via OpenRouter).

```bash
docker compose up --build
```

| serviço    | papel                                                                          |
| ---------- | ------------------------------------------------------------------------------ |
| `postgres` | PostgreSQL 17; o init cria também `modernizer_test` e `modernizer_eval`        |
| `migrate`  | `alembic upgrade head`, uma vez, antes da app                                  |
| `app`      | `langgraph dev` em `:8000`: grafo, API do LangGraph, Studio e as rotas FastAPI |

```bash
curl -X POST localhost:8000/modernize -H "content-type: application/json" -d '{"source_code": "CREATE FUNCTION add_one(p int) RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN p + 1; END $$;"}'
```

OpenAPI em `http://localhost:8000/docs`, Studio em
`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:8000` e requisições prontas (os cinco
anexos com schema, erros, API do LangGraph) em [`docs/api.http`](docs/api.http).

> Windows: se `localhost` não responder, use `127.0.0.1` (o relay do WSL pode ocupar `[::1]:5432`).

### Local (uv)

```bash
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run langgraph dev --allow-blocking
```

`--allow-blocking`: o `asyncpg` lê `~/.postgresql` ao abrir conexões, e o `langgraph dev` acusa
esse I/O síncrono.

### Anexos B–F

`examples/schema.sql` (Anexo A) e `examples/procedures/*.sql` (B–F) são o material do desafio. O
script roda os cinco pelo mesmo caso de uso do `POST /modernize`, avalia cada um e grava tudo:

```bash
uv run python -m scripts.run_examples                            # os cinco anexos
uv run python -m scripts.run_examples -p b_fn_saldo_cliente      # uma rotina
uv run python -m scripts.run_examples --without-generated-cases  # só os casos do dataset
uv run python -m scripts.run_examples --history                  # histórico das rodadas
```

Cada rodada fica em `examples/results/history/run_<id>_<tag>/` (nada é sobrescrito), com
`SUMMARY.md` e, por anexo, `payload.json`, `generated.py`, `report.json` e `evaluation.json`. Os
arquivos versionados são rodadas reais, sem curadoria.

---

## Pipeline

```mermaid
flowchart LR
    START((START)) --> record_start
    record_start --> parsing
    parsing --> semantic_analysis
    semantic_analysis --> code_generation
    semantic_analysis -. "generate_cases (padrão)" .-> case_generation
    code_generation --> validation
    case_generation --> validation
    validation -- "reprovado, tentativas e tempo sobrando" --> code_generation
    validation -- "aprovado ou sem retry" --> record_result
    record_result --> END((END))
```

| nó                  | o que faz                                                                                      | implementação           |
| ------------------- | ---------------------------------------------------------------------------------------------- | ----------------------- |
| `record_start`      | grava a execução como `running` antes do LLM                                                   | `ExecutionLog.start`    |
| `parsing`           | PL/pgSQL → IR `ParsedProcedure` (parâmetros, statements, SQL embutido, tabelas, locks)         | `PglastParser`          |
| `semantic_analysis` | construtos SQL, riscos (N+1, `FOR UPDATE`, exceção engolida, SQL dinâmico…) e estratégia       | `SemanticAnalyzer`      |
| `code_generation`   | prompt montado das etapas anteriores → LLM → contrato JSON                                     | `GenerateCode`          |
| `case_generation`   | em paralelo: o LLM propõe casos de teste, a rotina original filtra                             | `GenerateCases`         |
| `validation`        | `ast.parse` (bloqueante), Ruff e comportamento contra a rotina original (não bloqueantes)      | `ValidateCode`          |
| `record_result`     | calcula o status e grava código e relatório                                                    | `ExecutionLog.complete` |

- **Estado tipado** (`graph/state.py`): um `TypedDict` com o resultado de cada etapa e canais
  append-only para `completed_steps`, `warnings` e `errors`.
- **Nós finos** (`graph/nodes.py`): leem o estado, chamam um colaborador e devolvem a atualização.
  Nenhuma regra de negócio mora no grafo.
- **Falha em qualquer etapa**: um wrapper grava `failure` com o que já foi produzido e relança; o
  handler global converte em HTTP. Vale para `POST /modernize`, API do LangGraph e Studio.
- **Loop de reparo**: se algum check reprova, a geração roda de novo com o código anterior e os
  achados no prompt. Limites: `CODE_GENERATION_MAX_ATTEMPTS` (2) e
  `CODE_GENERATION_RETRY_BUDGET_SECONDS` (90 s), porque a request é síncrona.
- **Fan-out**: `code_generation` e `case_generation` rodam no mesmo superstep; `validation` espera
  os dois. O reparo volta só para `code_generation`. `case_generation` nunca derruba a execução:
  falha vira warning.

### Verificação do código gerado

```mermaid
flowchart TD
    code["código gerado"] --> gather{{"ValidateCode: checks em paralelo"}}
    gather --> ast["python_ast · bloqueante"]
    gather --> ruff["ruff · não bloqueante"]
    gather --> beh["behavior · não bloqueante"]
    ast & ruff & beh --> result["ValidationResult"]
    result -- "algum check reprovou,<br/>tentativas e tempo sobrando" --> regen["code_generation<br/>código anterior + achados no prompt"]
    regen --> code
    result -- "tudo aprovado, ou sem retry" --> status["record_result<br/>bloqueante reprovou → failure<br/>não bloqueante reprovou ou pulado → partial<br/>senão → success"]
```

Um check pulado (sem casos, sem banco de avaliação) não dispara reparo, mas deixa o status em
`partial`: código que não foi executado contra o original não sai como `success`.

O check `behavior` por dentro (`validation/checks/behavior/`):

```mermaid
sequenceDiagram
    participant C as BehaviorCheck<br/>check.py
    participant H as BehavioralEquivalence<br/>harness.py (servidor)
    participant R as CaseRunner<br/>runner.py (subprocesso)
    participant DB as Postgres<br/>modernizer_eval
    C->>H: run(rotina, código, cenário)
    H->>R: python -m …runner, request JSON no stdin
    R->>R: load_entry_point: exec do código gerado (generated.py)
    loop cada caso
        R->>DB: CREATE SCHEMA eval_{uuid} + setup_sql + rotina original (sandbox.py)
        R->>DB: original: case.sql, conexão própria, rollback
        R->>DB: gerado: await entry(conn, *args), conexão própria, rollback
        Note over R: snapshot das tabelas em cada lado<br/>compare: desfecho · linhas · estado final (comparison.py)
        R->>DB: DROP SCHEMA
    end
    R-->>H: stdout "@@evaluation-result@@ [CaseResult…]"
    H-->>C: um CaseResult por caso
    C->>C: cada divergência vira achado BEHAVIOR (feedback do reparo)
```

- **Isolamento**: o código gerado só executa em `runner.py`, num subprocesso com timeout por caso
  e total. Crash, travamento ou estado vazado não chegam ao servidor.
- **Mesmo ponto de partida**: cada caso tem schema próprio e cada lado roda numa transação
  revertida.
- **Equivalente** = mesmo desfecho (os dois retornam, ou os dois lançam, e o Python com uma
  exceção definida no próprio módulo), mesmas linhas de resultado e mesmo estado final das tabelas.

---

## Arquitetura

Package by feature e, dentro da feature, uma pasta por etapa. Cada `domain.py` é o que a etapa
produz (Pydantic puro); `models.py` é a tabela.

```
app/
├── main.py                     # FastAPI app (referenciada pelo langgraph.json)
├── core/                       # composition root (dishka), config, banco, handlers HTTP
├── shared/
│   ├── errors.py               # AppError · DomainError · NotFoundError
│   ├── resilience/             # CircuitBreaker
│   └── integrations/           # IntegrationError, retries, tracing; llm/ (port, gateway, providers)
└── features/
    ├── health/
    └── modernization/
        ├── routes.py · schemas.py · use_cases.py · domain.py (execução, relatório, status)
        ├── graph/                  # builder · nodes · state
        ├── parsing/                # IR · SQLParser · plpgsql.py (pglast)
        ├── analysis/               # analyzer · risks · catalog
        ├── code_generation/        # prompt · generate_code
        ├── case_generation/        # prompt · generate_cases
        ├── validation/             # validate_code (CodeCheck, Rule) · checks/ (syntax, lint, behavior/)
        ├── evaluation/             # a métrica: domain · models · repository
        └── persistence/            # o histórico: models · repository · execution_log
```

**Fluxo de uma request:** rota → caso de uso → grafo → nós → steps e strategies. LLM e
persistência entram por injeção.

**Abstração só onde há variação real:**

| eixo              | contrato                                          | implementações                                        | nos testes   |
| ----------------- | ------------------------------------------------- | ----------------------------------------------------- | ------------ |
| LLM               | `LLM` (port)                                      | `LLMGateway` → `OpenRouterProvider`, `OpenAIProvider` | `FakeLLM`    |
| Persistência      | `ModernizationRepository`, `EvaluationRepository` | SQLAlchemy                                            | em memória   |
| Dialeto de origem | `SQLParser`                                       | `PglastParser`                                        | parser real  |
| Checks do código  | `CodeCheck` + `Rule(check, blocking)`             | AST, Ruff, comportamento                              | checks reais |
| Métrica           | `EquivalenceMetric`                               | `BehavioralEquivalence`                               | `FakeMetric` |

O grafo não fica atrás de interface: os testes rodam o grafo real com fakes nas bordas.
`tests/unit/test_architecture.py` fixa as regras de dependência: cada biblioteca só é importada
pelo módulo que a encapsula, o domínio não importa infraestrutura e as features não importam o
composition root.

---

## Banco de dados

Schema criado só por Alembic (`migrations/versions/`):

| tabela                  | conteúdo                                                                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `modernization_history` | `id` UUIDv7, `source_code`, `schema_context`, `generated_code`, `report` JSONB, `status` (CHECK), datas; índice `(status, created_at)` |
| `evaluation_results`    | FK para a execução, rotina, prompt/modelo, validade estática, casos aprovados/totais, score, casos em JSONB; índice `(procedure_name, created_at)` |

**Toda execução é persistida**, em dois commits: `running` antes do LLM (se o processo morrer, a
execução fica visível) e o desfecho no fim. Os repositórios abrem uma sessão por operação, então
nenhuma conexão fica presa durante a geração.

`modernizer_eval` só recebe os schemas descartáveis da verificação. Em um volume criado antes dele:

```bash
docker compose exec postgres psql -U modernizer -d postgres -c "CREATE DATABASE modernizer_eval OWNER modernizer"
```

---

## API e relatório

| método | rota                              | descrição                                             |
| ------ | --------------------------------- | ----------------------------------------------------- |
| GET    | `/health`                         | `{"status": "ok"}`                                    |
| POST   | `/modernize`                      | roda o pipeline e devolve código + relatório          |
| GET    | `/modernizations/{id}`            | uma execução gravada                                  |

Endpoints do experimento (bônus de métrica). Valem só para as rotinas do dataset, os Anexos B–F;
para outra rotina, o `POST` responde 400 (sem cenário). A verificação de uma request comum é o
check `behavior`, dentro do pipeline.

| método | rota                              | descrição                                             |
| ------ | --------------------------------- | ----------------------------------------------------- |
| POST   | `/modernizations/{id}/evaluation` | roda a métrica sobre uma execução e grava o resultado |
| GET    | `/evaluations`                    | última avaliação de cada rotina e as taxas agregadas  |

```json
{
  "source_code": "CREATE OR REPLACE FUNCTION fn_saldo_cliente ...",
  "schema": "CREATE TABLE contas (...);",
  "behavior": {
    "seed": "INSERT INTO contas VALUES (...);",
    "cases": [{ "name": "cliente 1", "sql": "SELECT fn_saldo_cliente(1)", "args": [1] }],
    "ignore_columns": ["id"]
  },
  "generate_cases": true
}
```

Só `source_code` é obrigatório. `behavior` (exige `schema`) dá o seed e os casos para executar o
código gerado contra o original; `generate_cases` (padrão `true`, exige `schema`) soma casos
propostos pelo LLM. A resposta traz `execution_id`, `status`, `generated_code` e `report`, com uma
seção por etapa: `parsing`, `semantic_analysis` (construtos, riscos com linha, estratégia),
`code_generation` (decisões, modelo, `prompt_version`, tokens, latência), `case_generation` (casos
mantidos e descartados com o motivo), `validation` (resultado por check) e `warnings`/`errors`.

| status    | quando                                                                            |
| --------- | --------------------------------------------------------------------------------- |
| `running` | gravado antes do LLM                                                              |
| `success` | todos os checks rodaram e passaram                                                |
| `partial` | Python válido, mas Ruff acusou algo, um caso divergiu ou o comportamento não rodou |
| `failure` | exceção numa etapa (progresso preservado) ou Python inválido depois dos reparos   |

Erros viram HTTP pela classe: `DomainError` → 400, `NotFoundError` → 404, `IntegrationError` →
502, 503 com `Retry-After` (circuito aberto) ou 504; o resto → 500. O corpo traz o
`execution_id`, e a execução já está gravada como `failure`.

---

## Métrica de evaluation

**Equivalência comportamental.** Cada caso do
[dataset](examples/evaluation/scenarios.yml) roda na rotina original e no módulo gerado, pelo mesmo
harness da validação, e compara resultado e estado final das tabelas. Taxa principal: rotinas com
todos os casos aprovados. Taxa por caso, holdout, validade estática e conclusão saem à parte.

**Onde ver.** O `run_examples` avalia cada anexo e grava em `evaluation_results`; o
`GET /evaluations` mostra a última avaliação de cada rotina e as taxas sobre B–F. Para reavaliar
uma execução gravada: `POST /modernizations/{id}/evaluation`.

**Por que essa métrica.** Na primeira versão do prompt, AST e Ruff aprovaram 5/5 módulos e só 1/5
se comportava como o original. Os defeitos eram de execução (bind sem tipo que o asyncpg rejeita,
`begin_nested()` sem `await`, arredondamento `NUMERIC` por atribuição, `UPDATE … FROM` aplicando
uma linha só) e só aparecem executando contra o original.

**Casos gerados.** O LLM propõe chamadas (e o seed, se o chamador não mandou) mas nunca o resultado
esperado: o oráculo é a rotina original, então um caso ruim no máximo é inútil. Cada caso passa por
dois filtros antes de entrar:

- **estático**: número de `args` igual ao de parâmetros de entrada, a SQL chama a rotina e só com
  literais (uma subquery na chamada faria os dois lados rodarem com entradas diferentes);
- **execução só no original** (`probe`): descarta SQL inválido para o schema (SQLSTATE 42),
  timeout e seed que quebra o setup. Um `RAISE` da rotina é comportamento e fica.

No Anexo F, foram dois casos gerados que pegaram os bugs da 1ª tentativa (datas `NULL` e período
invertido), corrigidos no reparo.

**Holdout.** O dataset separa **12 casos dev** de **9 holdout**. O `run_examples` manda só os dev
como `behavior`, e a métrica roda todos depois: a taxa de holdout mede se a correção generaliza, em
vez de só se ajustar aos casos que o LLM viu.

**Fora da métrica:** só as entradas do dataset (não é prova geral); NOTICE, concorrência e texto
exato de erro não são comparados; colunas de sequência e relógio são ignoradas.

### Resultado

Modelo `z-ai/glm-5.3-flash`, prompt `code-generation-v5`, Anexo A como schema, casos gerados ligados:

| rodada            | rotinas | casos     | holdout | validade AST | tokens (in / out) | duração |
| ----------------- | ------- | --------- | ------- | ------------ | ----------------- | ------- |
| `20261002_032528` | **5/5** | **21/21** | **9/9** | 100%         | 21,1k / 9,6k      | 269 s   |
| `20261002_150343` | **5/5** | **21/21** | **9/9** | 100%         | 18,8k / 13,8k     | 294 s   |

Temperatura 0 não torna o provider determinístico; por isso o holdout e mais de uma rodada.

### Decisões de tradução por anexo

Rodada [`20261002_032528`](examples/results/history/run_20261002_032528_run/) (tentativas: B 1, C 1,
D 1, E 2, F 2).

- **B** (`database_delegated`): uma consulta agregada em SQL; o resultado volta como `Decimal`
  arredondado com `quantize`, como a variável `NUMERIC(18,2)` do original.
- **C** (`hybrid`): `p_dias` validado em Python; o `UPDATE … NOT EXISTS` fica no banco;
  `GET DIAGNOSTICS` vira `result.rowcount`; o OUT vira uma dataclass congelada.
- **D** (`hybrid`): o corpo roda num savepoint (`async with conn.begin_nested()`), porque o
  `EXCEPTION WHEN OTHERS` original cobre também os `RAISE` de validação; `FOR UPDATE` e escritas
  na mesma conexão; uma exceção por `RAISE`; no handler, grava a auditoria de erro e relança.
- **E** (`hybrid`): o loop cursor-a-cursor (N+1) virou SQL set-based: uma tabela temporária calcula
  todas as tarifas de uma vez (taxa vigente por `LEFT JOIN LATERAL`, arredondamentos
  intermediários) e três statements em lote fazem débito, tarifas e auditoria. A 1ª tentativa
  falhava com data `NULL`; um caso gerado pegou.
- **F** (`hybrid`): CTE recursiva, agregações e a chamada a `fn_saldo_cliente` ficam em SQL;
  `RAISE NOTICE` vira `logging`. A 1ª tentativa validava o período fora do handler e comparava
  datas `NULL` em Python; a 2ª acertou os dois.

---

## Observabilidade com Langfuse

Langfuse v4 self-hosted ([`docker-compose.langfuse.yml`](docker-compose.langfuse.yml), Compose
oficial, portas só em localhost): nenhum dado sai da máquina.

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Preencha as variáveis `LANGFUSE_*` do `.env.example` (projeto, chaves e login são provisionados na
primeira subida). UI em `http://localhost:3000`. Sem as chaves, o tracing fica desligado.

| no trace                         | o que mostra                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------- |
| um span por nó                   | inclusive cada tentativa do loop de reparo (callback nativo do LangGraph)             |
| `llm.generate`                   | prompt, resposta, modelo, tokens, latência e **custo** (o valor cobrado pelo OpenRouter) |
| `behavior.run` (em `validation`) | um span por caso com SQL, args e o que cada lado fez; divergência em vermelho; score `behavior_pass_rate` |
| `behavior.probe` (em `case_generation`) | cada caso proposto, mantido ou descartado e por quê                            |

Os casos rodam no subprocesso, então seus spans são emitidos quando os resultados voltam: o tempo
real de cada caso está em `duration_ms`.

![Trace do Anexo D no Langfuse](docs/langfuse/langfuse_screenshot_1.png)

---

## Testes e qualidade

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Com Postgres (`docker compose up -d postgres`), os de integração rodam também:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://modernizer:modernizer@127.0.0.1:5432/modernizer_test EVALUATION_DATABASE_URL=postgresql+asyncpg://modernizer:modernizer@127.0.0.1:5432/modernizer_eval uv run pytest --cov
```

- **275 testes** (262 unitários, 13 de integração); nenhum chama um LLM real.
- **Cobertura 96%** com integração (branches e o subprocesso incluídos), `fail_under = 95`.
- **Unitários**: parser e análise (inclusive regressões dos anexos), contrato do LLM, checks e
  política de bloqueio, loop de reparo, filtros de casos gerados, persistência, API, gateway
  (failover, circuit breaker), tracing e regras de arquitetura.
- **Integração**: Postgres migrado por Alembic, repositórios, equivalência contra o PL/pgSQL real
  e o ciclo completo do reparo (um B errado corrigido pelo feedback dos casos).

---

## Decisões técnicas

**1. Parsing com pglast, sem regex.** O `pglast` usa o parser do próprio PostgreSQL: lê PL/pgSQL e
SQL exatamente como o banco (`sqlglot` e `sqlparse` não entendem o corpo PL/pgSQL). O resultado é
um IR próprio (`ParsedProcedure`); objetos do pglast não saem do adapter, e outro dialeto é outra
implementação de `SQLParser`.

**2. Análise determinística e a escolha SQL × Python.** O `SemanticAnalyzer` é lógica pura sobre o
IR. Sem SQL embutido → `python_reimplementation`; só SQL set-based → `database_delegated`; SQL com
controle procedural → `hybrid`. Princípio: **lógica relacional fica no banco** (joins, agregações,
CTEs, DML em massa e locks como SQL parametrizado); Python coordena validação, fluxo e erros.
Reescrever em Python puro mudaria arredondamento, NULL, locks e desempenho. O LLM pode divergir da
recomendação justificando; o relatório guarda as duas.

**3. Prompt a partir da análise.** O prompt leva assinatura, outline do fluxo com linhas, SQL
embutido com fatos extraídos, riscos, estratégia, schema e o source original. As regras cobrem as
armadilhas medidas (binds com `CAST`, savepoints com `async with`, `Decimal`). A resposta é JSON
validado por Pydantic; desvios que não afetam o código (cerca Markdown, decisão malformada) viram
warning em vez de custar uma tentativa. Um experimento sem o source no prompt
([resultados](examples/experiments/sem-source/HISTORY.md)) economizou 7–12% de input por tentativa,
mas errou o arredondamento do Anexo E nas duas rodadas e gastou mais tentativas: o source ficou.

**4. LLM atrás de um gateway.** As features só conhecem o port `LLM`. O `LLMGateway` tenta rotas
(provider + modelo) em ordem dentro de um orçamento de tempo; cada provider tem retries e circuit
breaker, que só contam falhas transitórias. Trocar de modelo é configuração. Com modelos de
raciocínio, `LLM_REASONING_EFFORT=low` é necessário: sem limite, o Anexo F gastou 32k tokens
pensando e voltou vazio.

**5. Validação: checks reportam, regras decidem.** Cada `CodeCheck` só devolve achados; o que
bloqueia é política, declarada uma vez em `core/providers.py`. Divergência de comportamento não
bloqueia porque o código continua sendo Python válido: o status `partial` e o relatório dizem qual
caso divergiu.

**6. Persistência dentro do grafo.** A gravação fica nos nós `record_start`/`record_result` e no
wrapper de falha, não no caso de uso, porque o grafo tem três entradas (`POST /modernize`, API do
LangGraph, Studio) e todas precisam gravar.

**7. Composition root com dishka.** Cada dependência é declarada uma vez em `core/providers.py`; o
container valida tudo no boot (chave de LLM ausente derruba o startup, não a primeira request). O
`langgraph dev` serve a API e o grafo no mesmo processo, com o mesmo container.

**8. Erros por papel.** A classe do erro diz quem é o culpado e o handler global deriva o status.
`try/except` local só onde o resultado precisa ser observado ali (lista fixada num teste).

**9. `langgraph dev` no container.** Serve grafo, API nativa, Studio e rotas FastAPI num processo,
sem dependências externas. `langgraph up` exige Redis, Postgres do runtime e licença; fica como
caminho de produção.

**10. Python 3.14 de ponta a ponta.** O código gerado é julgado e executado no mesmo 3.14 em que
o projeto roda, não só pedido no prompt:

| camada                       | onde                                                                   |
| ---------------------------- | ---------------------------------------------------------------------- |
| projeto e imagem             | `requires-python = ">=3.14"`, `FROM python:3.14-slim`, `langgraph.json` |
| prompt de geração            | "Target Python 3.14 with full type hints"                              |
| sintaxe do código gerado     | `ast.parse` no próprio interpretador 3.14                               |
| lint do código gerado        | Ruff com `--target-version=py314`                                       |
| equivalência contra o original | o subprocesso sobe com `sys.executable`, o mesmo 3.14 do servidor     |

O código gerado é 3.14 válido e com tipagem moderna (`int | None`, `list[...]`), mas não usa nada
exclusivo do 3.14: é acesso a banco, sem motivo para isso.

| escolha                   | ganho                                     | custo                                         |
| ------------------------- | ----------------------------------------- | --------------------------------------------- |
| LLM gera, regras checam   | traduz o que regras não cobririam         | não determinismo; exige validação e relatório |
| Validação por execução    | pega os defeitos que AST/Ruff não veem    | precisa de casos e de um banco descartável    |
| Holdout fora do loop      | a métrica continua medindo generalização  | menos feedback para o reparo                  |
| Dois commits por execução | rastreável mesmo se o processo morre      | estado `running` intermediário visível        |
| API síncrona              | simples de usar e testar                  | request longa (ver Escalabilidade)            |

---

## Escalabilidade

**Gargalo:** parsing e análise levam milissegundos; a geração leva de ~7 s a mais de 100 s.
Escalar o pipeline é escalar **chamadas concorrentes ao LLM** dentro do rate limit do provider.

**Já no código:** processo sem estado (tudo no Postgres), então réplicas atrás de um balanceador
funcionam sem coordenação; I/O async de ponta a ponta; nenhuma conexão presa durante o LLM; checks
e as duas gerações em paralelo; failover e circuit breaker no LLM.

**Próximos passos:**

- **Fila:** `POST /modernize` responde `202` com o `execution_id` (a linha `running` já existe) e o
  cliente consulta `GET /modernizations/{id}`. Workers consomem a fila (runtime do LangGraph em
  produção, ou SQS/RabbitMQ) com um semáforo por provider dimensionado pelo rate limit.
- **Cache:** `sha256(source normalizado + schema + modelo + versões de prompt)` numa coluna
  indexada; submissão repetida devolve o resultado sem chamar o LLM. O system prompt estático
  serve de prefixo para o prompt caching do provider.
- **Volume:** particionar `modernization_history` por mês e ler por réplica.

---

## Configuração

Os valores ficam só no `.env` (copie o [`.env.example`](.env.example), que documenta cada
variável). O `settings.py` não tem defaults: uma variável ausente impede o boot e aparece pelo
nome. No Docker, o compose só sobrescreve os endereços que mudam dentro do container.

| variável | no `.env.example` | descrição |
|---|---|---|
| `DATABASE_URL` | `…@localhost:5432/modernizer` | driver async |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | `openrouter` / `z-ai/glm-5.3-flash` / vazio | uma rota; sem chave o boot falha |
| `LLM_REASONING_EFFORT` | `low` | vazio = não enviado |
| `LLM_CONFIG_FILE` | vazio | YAML com várias rotas ([exemplo](config/llm.example.yml)) |
| `LLM_TEMPERATURE` / `LLM_MAX_OUTPUT_TOKENS` | `0.0` / `8192` | |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` / `LLM_CIRCUIT_BREAKER_*` | `120` / `2` / `5` falhas, `60` s | |
| `CODE_GENERATION_MAX_ATTEMPTS` / `…_RETRY_BUDGET_SECONDS` | `2` / `90` | 1 tentativa desliga o reparo |
| `EVALUATION_DATABASE_URL` | `…/modernizer_eval` | banco descartável; vazio = comportamento não verificado |
| `EVALUATION_DATASET_FILE` / `EVALUATION_CASE_TIMEOUT_SECONDS` | `examples/evaluation/scenarios.yml` / `10` | |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | vazio / vazio / `http://localhost:3000` | as duas chaves ligam o tracing |
| `TEST_DATABASE_URL` | `…/modernizer_test` | só testes de integração |

Também: `APP_NAME`, `LOG_LEVEL`, `DATABASE_ECHO`, `LLM_BASE_URL`, `RUFF_TIMEOUT_SECONDS`.

---

## Limitações e evolução

**Limitações:**

- Só `LANGUAGE plpgsql` e o primeiro `CREATE FUNCTION/PROCEDURE` do arquivo.
- Sem catálogo do banco: `%TYPE`/`%ROWTYPE` viram placeholder com warning, e builtin × rotina do
  usuário é heurística. `EXECUTE` (SQL dinâmico) vira o risco `DYNAMIC_SQL`.
- O comportamento só é verificado com casos (do `behavior` ou gerados, que exigem `schema`); sem
  isso o status fica `partial`.
- O mesmo modelo escreve o código e os casos, então os pontos cegos podem coincidir; cobertura de
  ramos não é medida.
- O subprocesso isola o servidor, mas não é um sandbox (mesma máquina, com rede).
- `POST /modernize` é síncrono.
- O Ruff do código gerado usa só regras de correção, sem as de modernização (`UP`): sintaxe
  antiga (`Optional[int]`) passaria. Nas duas rodadas do Resultado não há nenhuma (checagem
  manual), mas alguns módulos trazem `from __future__ import annotations`, redundante no 3.14.

**Com mais tempo:**

- Casos mais fortes: valores de borda tirados do IR de forma determinística, cobertura de ramos com
  `plpgsql_check` e outro modelo para os testes.
- Modo assíncrono com fila, cache e backpressure por provider.
- Código gerado em container sem rede e com papel de banco restrito.
- Resolver `%TYPE` e builtins pelo catálogo; novos dialetos (T-SQL, PL/SQL) via `SQLParser`.
