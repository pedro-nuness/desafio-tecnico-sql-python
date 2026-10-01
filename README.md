# PL/pgSQL Modernizer

Pipeline híbrido que moderniza stored procedures/functions **PL/pgSQL** para **Python 3.14**,
orquestrado com **LangGraph**:

```
PL/pgSQL → parsing determinístico → análise semântica determinística → geração via LLM
        → validação determinística → Python 3.14 + relatório estruturado (JSONB)
```

O objetivo não é um compilador PL/pgSQL completo, e sim uma arquitetura **extensível e testável**
onde as partes determinísticas (parser, análise, validação) dão contexto e garantias ao passo
não determinístico (LLM), e onde provider de LLM, parser, validadores e persistência são
substituíveis.

---

## Sumário

- [Quick start](#quick-start)
- [Arquitetura](#arquitetura)
- [Fluxo LangGraph](#fluxo-langgraph)
- [Escalabilidade](#escalabilidade)
- [Endpoints](#endpoints)
- [Relatório](#relatório)
- [Configuração](#configuração)
- [Observabilidade com Langfuse](#observabilidade-com-langfuse)
- [Migrations](#migrations)
- [Testes e qualidade](#testes-e-qualidade)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Decisões de tradução por anexo](#decisões-de-tradução-por-anexo)
- [Architectural Decisions](#architectural-decisions)
- [Limitações conhecidas](#limitações-conhecidas)
- [Evolução futura](#evolução-futura)

---

## Quick start

### Docker Compose (recomendado)

```bash
docker compose up --build
```

Sobe três serviços:

| serviço    | papel                                                                  |
|------------|------------------------------------------------------------------------|
| `postgres` | PostgreSQL 17 com healthcheck (`pg_isready`)                           |
| `migrate`  | job one-shot `alembic upgrade head` (espera o Postgres ficar healthy)  |
| `app`      | **servidor LangGraph CLI** (`langgraph dev`) em `:8000`: graph + API LangGraph + Studio + nossas rotas (só inicia após `migrate` terminar com 0) |

O compose lê o `.env` da raiz (copie de `.env.example`) para as variáveis de LLM:

```bash
LLM_PROVIDER=openrouter LLM_MODEL=anthropic/claude-sonnet-4.5 LLM_API_KEY=sk-or-... docker compose up --build
```

Teste rápido:

```bash
curl localhost:8000/health
```

```bash
curl -X POST localhost:8000/modernize -H "content-type: application/json" -d '{"source_code": "CREATE FUNCTION add_one(p int) RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN p + 1; END $$;"}'
```

Mesmo pipeline pela API nativa do LangGraph (também gravado em `modernization_history`):

```bash
curl -X POST localhost:8000/runs/wait -H "content-type: application/json" -d '{"assistant_id": "modernization", "input": {"source_code": "CREATE FUNCTION add_one(p int) RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN p + 1; END $$;"}}'
```

Studio: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:8000`. Docs da API:
`http://localhost:8000/docs`.

Todas as requisições prontas (health, os cinco anexos com schema, consulta por id, casos de erro,
API do LangGraph com e sem streaming) em [`docs/api.http`](docs/api.http), para a extensão
[REST Client](https://marketplace.visualstudio.com/items?itemName=humao.rest-client) do VS Code.

> Windows: se `localhost` não responder, use `127.0.0.1` — outro serviço (ex.: relay do WSL) pode
> estar ocupando a porta em IPv6 (`[::1]`).

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

(Alternativa só com as rotas FastAPI, sem API/Studio do LangGraph:
`uv run uvicorn app.main:app --reload`.)

### Servidor LangGraph (`langgraph dev` + Studio)

É o mesmo servidor do container `app`. O `langgraph.json` registra o graph `modernization` (factory `app.core.bootstrap:make_graph`) e monta
a própria API FastAPI via `http.app` (`app.main:app`), então o mesmo servidor expõe:

- API do LangGraph (`/assistants`, `/threads`, `/runs/...`) e o Studio — runs disparadas por aqui
  (input `{"source_code": "...", "schema_context": "..."}`) também são gravadas em
  `modernization_history`, porque a persistência está no próprio graph;
- `GET /health`, `POST /modernize`, `GET /modernizations/{id}` (nossas rotas, com lifespan combinado).

> `--allow-blocking`: o `langgraph dev` usa *blockbuster* para acusar I/O síncrono no event loop.
> O `asyncpg` resolve `~/.postgresql` (defaults de SSL) ao abrir cada conexão do pool, o que é
> acusado como bloqueante. É interno do driver e custa microssegundos (no uvicorn não há essa checagem).

### Anexos B–F (casos de teste do desafio)

`examples/schema.sql` (Anexo A) e `examples/procedures/*.sql` (Anexos B–F) são o material do
desafio. O script roda os cinco pelo mesmo caso de uso do `POST /modernize` (portanto cada
execução também é persistida em `modernization_history`), com o schema como contexto:

```bash
uv run python -m scripts.run_examples
```

Saída em `examples/results/`: `<procedure>/generated.py`, `<procedure>/report.json`, `<procedure>/evaluation.json` e
[`SUMMARY.md`](examples/results/SUMMARY.md) (status, tentativas, estratégia, riscos, Ruff, tokens,
duração, equivalência e holdout). Requer `EVALUATION_DATABASE_URL`: o pipeline roda os 12 casos
dev no loop de reparo (AD-17) e, depois de cada execução, a avaliação roda os 21 casos (dev +
holdout) e persiste o resultado em `evaluation_results`.
Os resultados versionados são uma execução real, **não curada**: falhas e `partial` ficam como
saíram, porque mostram o que a validação pega.

---

## Arquitetura

Package by Feature e, dentro da feature, **pastas por capacidade** (`parsing`, `generation`,
`validation`, `persistence`, `evaluation`, `graph`): contrato, lógica e implementações de uma capacidade ficam
juntos. Abstração só onde há variação real (AD-15): 5 ports (fronteiras com sistemas externos,
trocadas por fakes nos testes) e 2 strategies (algoritmos intercambiáveis).

```mermaid
flowchart LR
    Routes --> ModernizeRoutine
    Routes --> GetModernization
    ModernizeRoutine --> LangGraph
    LangGraph --> RecordStart
    RecordStart --> Parsing
    Parsing --> Analysis
    Analysis --> Generation
    Generation --> Validation
    Validation -. retry .-> Generation
    Validation --> RecordResult

    Parsing --> SQLParser
    Analysis --> SemanticAnalyzer
    Generation --> GenerateCode
    GenerateCode --> GenerationPromptBuilder
    GenerateCode --> LLM
    Validation --> ValidateCode
    ValidateCode --> CodeCheck

    RecordStart --> ExecutionLog
    RecordResult --> ExecutionLog
    ExecutionLog --> TransactionManager
    ExecutionLog --> Repository
    GetModernization --> TransactionManager
    GetModernization --> Repository
    Repository --> PostgreSQL
```

### Contratos por papel

| papel               | contrato                                                                         | onde                              |
|---------------------|----------------------------------------------------------------------------------|-----------------------------------|
| Rota (controller)   | `request.to_command()` → `use_case.execute(command)` → `Response.from_domain()`  | `routes.py`, `schemas.py`         |
| Caso de uso         | uma classe, um `async def execute(command)`; Command/Query são dataclasses frozen | `use_cases.py`                    |
| Nó do graph         | `StepNode`: `step: ClassVar[PipelineStep]` + `async __call__(state) -> StateUpdate`; pré-condições com `require()` | `graph/nodes.py` |
| Step                | classe com um `execute(...)`: `GenerateCode`, `ValidateCode`                     | `generation/`, `validation/`      |
| Strategy            | `SQLParser.parse(source)`, `CodeCheck.check(code) -> achados`                    | `parsing/`, `validation/`         |
| Port                | `LLM`, `TransactionManager`, `ModernizationRepository`, `EvaluationRepository`, `EquivalenceMetric` | `shared/`, `persistence/`, `evaluation/` |
| Ciclo de vida da run | `ExecutionLog.start / complete / fail`, uma transação cada                      | `persistence/execution_log.py`    |

### Eixos de variação (onde existem abstrações — e só lá)

| eixo                | tipo     | contrato                                         | implementações hoje                                   | nos testes |
|---------------------|----------|--------------------------------------------------|-------------------------------------------------------|------------|
| LLM provider/modelo | port     | `LLM`                                            | `LLMGateway` (routes em ordem → `OpenAIProvider` / `OpenRouterProvider`) | `FakeLLM` |
| Persistência        | port     | `TransactionManager` + `ModernizationRepository` | `SessionTransactionManager`, `SqlAlchemyModernizationRepository` | `InMemoryDatabase`, `InMemoryModernizationRepository` |
| Dialeto de origem   | strategy | `SQLParser`                                      | `PglastParser` (PL/pgSQL)                             | parser real |
| Checagens do código | strategy | `CodeCheck` + `Rule(check, blocking)`            | `PythonASTCheck`, `RuffCheck`                         | checks reais e de teste |

O orquestrador (LangGraph) **não** fica atrás de um port: é o fluxo da feature, e o caso de uso o
chama via `run_modernization` (AD-09). Não há `BaseService`, `BaseRepository`, `GenericDAO` etc. O
único "base" é `ValueObject` (configuração Pydantic `frozen`) e o `DeclarativeBase` do SQLAlchemy.

Direção das dependências (verificada por teste — `tests/unit/test_architecture.py`):

```
shared (reutilizável, zero dependências de core ou features)
core (providers, bootstrap, server, config, database) ──► features
features/modernization:
  routes/schemas ──► use_cases ──► graph ──► generation · validation · parsing (steps/strategies)
                                      └────► persistence (ExecutionLog, repository)
  domain ◄── todos; o domínio não importa nada da feature
  cada lib só no módulo que a encapsula: pglast → parsing/plpgsql.py · ruff → validation/ruff_check.py
  · sqlalchemy → persistence/models.py · langgraph → graph/builder.py
app/core/providers.py = composition root (único lugar que conhece todas as implementações);
features nunca importam providers/bootstrap/server
```

---

## Fluxo LangGraph

```mermaid
flowchart LR
    START((START)) --> record_start
    record_start --> parsing
    parsing -- ok --> semantic_analysis
    semantic_analysis -- ok --> generation
    generation -- ok --> validation
    validation -- "reprovado · tentativas < máx · dentro do orçamento" --> generation
    validation -- "aprovado ou sem retry" --> record_result
    parsing -. falhou .-> record_result
    semantic_analysis -. falhou .-> record_result
    generation -. falhou .-> record_result
    record_result --> END((END))
```

- **Estado tipado** (`app/features/modernization/graph/state.py`): `ModernizationState` (`TypedDict`) com modelos de domínio
  explícitos (`ParsedProcedure`, `SemanticAnalysis`, `GenerationResult`, `ValidationResult`) e canais
  append-only (`completed_steps`, `warnings`, `errors`) via reducer `operator.add`.
- **Nodes finos** (`app/features/modernization/graph/nodes.py`): cada um implementa `StepNode`,
  chama uma strategy/step e devolve `StateUpdate`; pré-condições via `require()`; exceções
  propagam sem tratamento HTTP local.
- **Persistência**: `record_start` commita `running` antes do LLM; `record_result` grava a
  conclusão normal. Se um passo lança exceção, o wrapper `_tracked` grava `failure`
  (`ExecutionLog.fail`) com o progresso já realizado e relança — vale para `POST /modernize`, API do LangGraph e Studio.
- **Loop de reparo** (`validation → generation`, AD-13): se a validação reprova (AST, Ruff ou
  o check de comportamento, AD-17), a geração roda de novo com o código anterior e a lista de
  problemas no prompt, até
  `GENERATION_MAX_ATTEMPTS` (default 2) e só se a run tiver menos de
  `GENERATION_RETRY_BUDGET_SECONDS` (default 90 s). `completed_steps` mostra cada tentativa
  (`…generation, validation, generation, validation`) e `report.generation.attempt` diz qual
  tentativa produziu o código final.
- **Dependências injetadas nos nodes** (instâncias callable construídas no builder), não via
  `config`/`context` — assim o mesmo graph roda no uvicorn e no servidor LangGraph.
- **Tratamento global** (AD-14): erros viram resposta HTTP só em
  `app/core/exception_handlers.py`, que não conhece nenhum vendor. `try/except` local existe
  apenas onde o resultado precisa ser observado ali mesmo: tradução de erros de SDK + retries
  (`Integration`), estado do breaker, gravação da falha no graph, e erros nativos com
  significado de domínio (SQL inválido, sintaxe inválida que alimenta o reparo, resposta do
  LLM fora do contrato). A lista é fixada em `test_architecture.py`.

---

## Escalabilidade

**Onde está o gargalo (medido nos anexos):** parsing e análise semântica levam milissegundos; a
geração leva de 25,6 s a 99,9 s por procedure na rodada v3 (`examples/results/SUMMARY.md`). Escalar este
pipeline é, portanto, escalar **chamadas concorrentes ao LLM** dentro dos rate limits dos
providers — CPU e banco não são o limite. As propostas abaixo partem disso.

### O que já está no código

| eixo | como |
|---|---|
| Réplicas horizontais | Processo sem estado: tudo o que importa está no Postgres (`modernization_history`). N réplicas atrás de um balanceador servem `POST /modernize` e `GET /modernizations/{id}` sem coordenação. |
| Conexões | Um engine (pool async do SQLAlchemy: 5 + 10 overflow por processo) compartilhado pela API e pelo servidor LangGraph (AD-11). Transações curtas: nenhuma conexão fica presa durante a chamada ao LLM (AD-07/AD-08), então o pool não limita a concorrência de runs. |
| I/O | Async de ponta a ponta (FastAPI, asyncpg, SDKs de LLM async). Ruff roda em worker thread, sem bloquear o event loop. |
| Paralelização dentro da run | Checks de validação rodam em paralelo (`ValidateCode`, AD-06). As etapas são sequenciais por dependência de dados (parse → análise → geração → validação). |
| Paralelização entre runs | Runs independentes rodam concorrentes: `scripts/run_examples.py` dispara os 5 anexos com `asyncio.gather`, como requests simultâneas. |
| Resiliência do LLM | `LLMGateway` com failover entre providers e modelos, circuit breaker por provider, retries com backoff e orçamento de tempo por chamada (AD-04): pico de erro num provider não derruba o pipeline. |
| Novos dialetos / LLMs / checks | Uma strategy `SQLParser` por dialeto; um provider declarado no YAML do gateway; um `CodeCheck` + `Rule` (AD-15). Nada existente muda. |

### Filas (próximo passo para volume)

O `POST /modernize` é síncrono: a request espera o LLM. Para volume:

1. **Modo assíncrono**: `POST /modernize` grava a run como `running` (o `record_start` já faz isso)
   e responde `202 Accepted` com o `execution_id`; o cliente consulta `GET /modernizations/{id}`,
   que já existe. O trabalho vai para uma fila.
2. **Fila e workers**: o próprio servidor LangGraph já tem fila de runs (`POST /threads/{id}/runs`
   em background). Em produção (`langgraph build`: Redis + Postgres) os workers escalam
   independentemente da API. Alternativa sem o runtime do LangGraph: uma fila (SQS, RabbitMQ,
   ou Redis com `arq`) com workers que chamam o mesmo `ModernizeRoutine`.
3. **Backpressure por provider**: um semáforo por provider no `Integration` (ao lado do circuit
   breaker), dimensionado pelo rate limit contratado. Requests acima disso esperam na fila em
   vez de tomar `429`.

### Cache

- **Resultado (idempotência)**: chave `sha256(source normalizado + schema + rota de LLM +
  PROMPT_VERSION + regras de validação)`, numa coluna indexada de `modernization_history`. Uma
  submissão repetida devolve o resultado `success` existente sem chamar o LLM. A execução continua
  sendo registrada (como cache hit), porque o requisito é persistir toda execução.
- **Prompt caching do provider**: o system prompt é estático (`PROMPT_VERSION`) e vem primeiro,
  então é um prefixo reaproveitável. A OpenAI cacheia prefixos longos automaticamente; outros
  providers (ex.: Anthropic) pedem marcação explícita no request, um ajuste no adapter.
- **Não cachear** parsing e análise: são determinísticos e custam milissegundos.

### Banco em volume

O índice `(status, created_at)` já atende a listagem por status. Com volume: particionar
`modernization_history` por mês (`created_at`), definir retenção para o `report` JSONB e servir o
`GET` por réplica de leitura.

---

## Endpoints

| Método | Rota                         | Descrição                                        |
|--------|------------------------------|--------------------------------------------------|
| GET    | `/health`                    | `{"status": "ok"}`                               |
| POST   | `/modernize`                 | executa o pipeline e persiste a execução         |
| GET    | `/modernizations/{id}`       | recupera uma execução persistida (404 se não existe) |
| POST | `/modernizations/{id}/evaluation` | avalia uma execução gravada e persiste o resultado |
| GET | `/evaluations` | última avaliação por rotina e taxas agregadas |

`POST /modernizations/{id}/evaluation` não recebe corpo. Retorna `score` (casos aprovados /
totais), `cases_passed`, `cases_total`, `equivalent` (todos passaram), `static_valid` (AST),
`completed` (terminou com código), prompt/modelo e detalhe por caso (`original`, `generated`,
`detail`). Rotina fora do dataset → 400; execução ausente → 404; banco não configurado → 500.

`GET /evaluations` retorna `routines`, `equivalence_rate` (rotinas equivalentes / avaliadas),
`case_pass_rate`, `static_valid_rate`, `completion_rate` e `evaluations`. Taxas de 0 a 1. Usa a
avaliação mais recente de cada rotina, podendo misturar prompts; os artefatos das rodadas
preservam a comparação por versão.

`POST /modernize`

```json
{ "source_code": "CREATE OR REPLACE FUNCTION ...", "schema": "CREATE TABLE orders (...);" }
```

`schema` é opcional. A resposta:

```json
{
  "execution_id": "01a0f3fe-...",
  "status": "success",
  "generated_code": "from sqlalchemy import text\n...",
  "report": {
    "parsing": {}, "semantic_analysis": {}, "generation": {}, "validation": {},
    "completed_steps": ["parsing", "semantic_analysis", "generation", "validation"],
    "errors": [], "warnings": []
  },
  "created_at": "...", "updated_at": "..."
}
```

Erros do pipeline viram erros HTTP pela classe (AD-14): `DomainError` (SQL/PL/pgSQL inválido
ou não suportado) → 400; `NotFoundError` → 404; `IntegrationError` → 502, 503 com `Retry-After`
(circuito aberto) ou 504 (timeout); `AppError` → 500 com a mensagem; qualquer outra exceção →
500 genérico. O corpo é `{"detail": mensagem, ...payload, "execution_id": ...}`. O graph já
gravou `failure` e o progresso (incluindo código já gerado, e o mesmo payload em
`report.errors[].payload`); o relatório fica disponível no GET da execução. Resultados de validação continuam alimentando
o loop de reparo; erros de execução dos validadores propagam. Entrada inválida → 422.
Se o banco não permitir gravar a falha, o erro de persistência propaga (500).

---

## Relatório

`ModernizationReport` (domínio, Pydantic, serializado com `model_dump(mode="json")` para JSONB):

| seção               | conteúdo                                                                                          |
|---------------------|---------------------------------------------------------------------------------------------------|
| `parsing`           | `success`, `procedure_name`, `kind`, `parser`, parâmetros/modos, `return_type`, tabelas, funções, `warnings` |
| `semantic_analysis` | `detected_features` (com ocorrências e linhas), `risks` (`code`, `severity`, `message`, `line`), `dependencies`, `recommended_strategy`, `recommendations` |
| `generation`        | `strategy` (escolhida pelo LLM), `recommended_strategy` (determinística), `provider`, `model`, `prompt_version`, tokens, `latency_ms`, `finish_reason`, `attempt` (tentativa que produziu o código), `architectural_decisions`, `warnings` |
| `validation`        | `valid_python`, `passed_all`, resultado por validador (`validator`, `success`, `blocking`, `messages`), `warnings` |
| raiz                | `completed_steps` (repete `generation`/`validation` a cada retentativa), `errors` (`step`, `error_type`, `message`), `warnings` |

### Status

| status    | quando                                                                              |
|-----------|-------------------------------------------------------------------------------------|
| `running` | gravado pelo nó `record_start`, **antes** do LLM (se o processo morrer, a execução fica visível) |
| `success` | código gerado e todos os validadores passaram                                        |
| `partial` | código gerado e sintaticamente válido, mas Ruff reportou algo, o comportamento divergiu do original em algum caso dev (AD-17), ou a validação não rodou |
| `failure` | execução interrompida por exceção (progresso preservado) ou Python inválido após reparos |

A regra fica em `PipelineOutcome.status()` (domínio), testada isoladamente.

---

## Configuração

Centralizada em `app/core/config/settings.py` (`pydantic-settings`; lê env vars e `.env`). Nenhum
`os.getenv` espalhado. Copie `.env.example` para `.env` (git-ignored).

| variável                | default                                                       | descrição                                  |
|-------------------------|---------------------------------------------------------------|--------------------------------------------|
| `DATABASE_URL`          | `postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer` | driver async obrigatório          |
| `LLM_CONFIG_FILE`       | —                                                             | YAML com providers e routes (AD-04, `config/llm.example.yml`); quando definido, as `LLM_*` abaixo, exceto temperatura e tokens, são ignoradas |
| `LLM_PROVIDER`          | `openrouter`                                                  | `openai` \| `openrouter`                   |
| `LLM_MODEL`             | `anthropic/claude-sonnet-4.5`                                 | ex.: `anthropic/claude-sonnet-4.5` (OpenRouter), `gpt-...` (OpenAI) |
| `LLM_API_KEY`           | —                                                             | obrigatório para `openai`/`openrouter` (falha no startup se ausente) |
| `LLM_BASE_URL`          | —                                                             | qualquer endpoint OpenAI-compatible        |
| `LLM_TEMPERATURE`       | `0.0`                                                         |                                            |
| `LLM_MAX_OUTPUT_TOKENS` | `8192`                                                        |                                            |
| `LLM_TIMEOUT_SECONDS`   | `120`                                                         |                                            |
| `LLM_MAX_RETRIES`       | `2`                                                           | tentativas adicionais: SDK OpenAI / adapter OpenRouter |
| `LLM_REASONING_EFFORT`  | — (não enviado)                                               | `minimal`\|`low`\|`medium`\|`high`, só para modelos de raciocínio (ver AD-04) |
| `LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5`                                           | falhas consecutivas (após retries) que abrem o circuito |
| `LLM_CIRCUIT_BREAKER_RESET_SECONDS` | `60`                                              | tempo em fail-fast antes de uma chamada de teste (half-open) |
| `GENERATION_MAX_ATTEMPTS` | `2`                                                         | tentativas de geração (1 = sem loop de reparo) |
| `GENERATION_RETRY_BUDGET_SECONDS` | `90`                                                | nenhuma retentativa começa depois disso (limita a espera síncrona) |
| `RUFF_TIMEOUT_SECONDS`  | `20`                                                          |                                            |
| `LOG_LEVEL`             | `INFO`                                                        |                                            |
| `EVALUATION_DATABASE_URL` | — | banco descartável separado; executa o Python gerado (AD-16) |
| `EVALUATION_DATASET_FILE` | `examples/evaluation/scenarios.yml` | schema, seed e casos por rotina |
| `EVALUATION_CASE_TIMEOUT_SECONDS` | `10` | timeout da chamada de cada lado em cada caso |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — | ambas habilitam traces do graph e LLM |
| `LANGFUSE_BASE_URL` | `http://localhost:3000` | SDK; Docker usa `LANGFUSE_DOCKER_BASE_URL` |
| `TEST_DATABASE_URL`     | —                                                             | só testes de integração                    |

---

## Observabilidade com Langfuse

Escolha: **self-hosted local (Langfuse v4)**, seguindo o
[Compose oficial](https://langfuse.com/self-hosting/deployment/docker-compose). Stack
opcional em `docker-compose.langfuse.yml`: Web, Worker, PostgreSQL, ClickHouse, Redis e
MinIO com volumes próprios. UI e MinIO são expostos somente em localhost.

Preencha as variáveis `LANGFUSE_*` de `.env.example` no `.env`: chaves `pk-lf-...`/`sk-lf-...`,
segredos aleatórios e `LANGFUSE_LOCAL_ENCRYPTION_KEY` com 64 caracteres hexadecimais.
`LANGFUSE_LOCAL_USER_*` define o login; organização, projeto e chaves são provisionados
na primeira inicialização ([documentação](https://langfuse.com/self-hosting/administration/headless-initialization)).

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Abra `http://localhost:3000` e entre com o login do `.env`. O SDK usa `LANGFUSE_BASE_URL`;
a aplicação Docker usa `LANGFUSE_DOCKER_BASE_URL` (default
`http://host.docker.internal:3000`). Sem ambas as chaves, tracing fica desabilitado.
Para Cloud, altere URL/chaves e dispense a stack local.

O callback nativo registra graph, etapas e reparos. `TracedLLM` registra prompt completo,
resposta, modelo e tokens como geração filha; o SDK registra duração e erros.
O container encerra o cliente com flush ao fechar. Instrumentação não altera o contrato do
LLM nem decide equivalência. Prompt e código ficam no destino configurado.

Evidência da rodada real v3: [lista de traces](output/playwright/langfuse-traces-v3.png) e
[geração com prompt, modelo e tokens](output/playwright/langfuse-generation-v3.png).
O [trace completo de B](output/playwright/langfuse-pipeline-v3.png) vem de uma execução
adicional para verificar a ligação da geração ao nó `generation`, após o ajuste do callback;
os artefatos e métricas B–F continuam sendo os da rodada comparativa acima.

---

## Migrations

Schema criado **somente** por Alembic: `0001` cria `modernization_history`; `0002` cria
`evaluation_results` (`migrations/versions/`);
`Base.metadata.create_all` não é usado em lugar nenhum (nem nos testes).
`migrations/env.py` é async e lê a URL do `Settings` (fonte única de configuração).

```bash
uv run alembic upgrade head
```

```bash
uv run alembic check
```

Tabela `modernization_history`: `id UUID` (UUIDv7, ordenável por tempo), `source_code`,
`schema_context`, `generated_code` (nullable), `report JSONB`, `status` (CHECK constraint),
`created_at`/`updated_at` `timestamptz`, índice `(status, created_at)`.

Tabela `evaluation_results`: FK para a execução (delete cascade), rotina, métrica,
prompt/modelo, validade estática, conclusão, casos aprovados/totais, score, detalhe JSONB
e data. Índice `(procedure_name, created_at)`.

O banco `modernizer_eval` recebe somente schemas descartáveis. Para volumes já existentes
(os scripts de init só rodam na primeira criação), crie-o uma vez:

```bash
docker compose exec postgres psql -U modernizer -d postgres -c "CREATE DATABASE modernizer_eval OWNER modernizer"
```

---

## Testes e qualidade

```bash
uv run pytest
```

```bash
docker compose up -d postgres
```

```bash
TEST_DATABASE_URL=postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer_test uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

```bash
uv run pytest --cov
```

**Cobertura (`pytest-cov`, branches incluídos): 96%** com os testes de integração; `fail_under = 95` em `pyproject.toml` faz `pytest --cov` falhar abaixo disso. O que
sobra descoberto é majoritariamente ramo defensivo do parser (nós PL/pgSQL que os anexos não usam).

- **Unitários (216)**: parser (`pglast`), análise semântica (inclusive sobre IR montado à mão, sem
  parser, e regressões medidas nos Anexos D–F), `GenerateCode` com test double (verifica que o prompt carrega parsing + análise;
  resposta truncada; `strategy` ausente), validação (checks AST e Ruff, política bloqueante/não
  bloqueante decidida pela `Rule`, checks em paralelo), `ExecutionLog`, casos de uso
  (`ModernizeRoutine`, `GetModernization`) com LangGraph real + banco em memória (sucesso, falha
  do LLM, falha de parsing, Python inválido, lint → partial, crash inesperado), graph (loop de
  reparo com feedback, limite de tentativas, orçamento de tempo, retentativa que falha mantém a
  anterior, run iniciada direto no graph — caminho do servidor LangGraph — é persistida),
  transação corrente por task, API (`/health`, `/modernize`, `/modernizations/{id}`), container e
  **regras de arquitetura** (imports via `ast`: dependências entre módulos, cada lib só no módulo
  que a encapsula, um `execute` por caso de uso).
- **Integração (10)**: Postgres real migrado com Alembic (subprocess), round-trip do repository,
  JSONB, rollback sem commit, persistência de sucesso **e** falha ponta a ponta, equivalência
  contra PL/pgSQL, isolamento/limpeza dos schemas e última avaliação por rotina.
  São pulados quando `TEST_DATABASE_URL` não está definido. O `docker compose` cria o banco
  `modernizer_test` (`docker/postgres/init`).
- Nenhum teste chama OpenAI/OpenRouter.

---

## Estrutura de pastas

A base de código adota a arquitetura **Package by Feature**, organizada em `core/`, `shared/` e `features/`, utilizando *native namespace packages* do Python 3 (sem arquivos `__init__.py`):

```
app/
├── main.py                   # Ponto de entrada FastAPI (reexporta create_app e app)
├── core/                     # Fundações transversais, infraestrutura base e composition root
│   ├── providers.py          # Composition root: providers dishka (AD-11)
│   ├── bootstrap.py          # Container dishka por processo, make_graph para langgraph.json
│   ├── config/settings.py    # Configurações centralizadas com Pydantic Settings
│   ├── database/             # SQLAlchemy async engine, session factory e Base declarativa
│   │   ├── base.py
│   │   ├── engine.py
│   │   ├── session.py
│   │   └── transaction.py    # SessionTransactionManager: transação corrente por task (AD-08)
│   ├── exception_handlers.py # Handlers globais de erro HTTP
│   └── server.py             # Setup da aplicação FastAPI e lifespan
├── shared/                   # Código utilitário compartilhado entre múltiplos contextos
│   ├── errors.py             # AppError, DomainError, NotFoundError (AD-14)
│   ├── persistence.py        # TransactionManager / Transaction: port de transação (AD-08)
│   ├── domain/value_object.py# Base ValueObject imutável (Pydantic frozen)
│   ├── resilience/           # CircuitBreaker genérico (async, sem dependência de vendor)
│   └── integrations/
│       ├── errors.py         # IntegrationError (transient, timeout, retry_after)
│       ├── integration.py    # Integration: tradução de erros de SDK + retries + breaker
│       └── llm/              # Port LLM + gateway com providers declarados e routes
│           ├── llm.py        # LLM (port das features), LLMRequest/LLMResponse
│           ├── config.py     # LLMSettings: providers + routes (YAML ou env)
│           ├── gateway.py    # LLMGateway: orquestrador (rotas em ordem, failover, orçamento)
│           ├── registry.py   # tipo de provider -> adapter, um Integration por provider
│           ├── openai/       # OpenAIProvider (Chat Completions, qualquer endpoint compatível)
│           └── openrouter/   # OpenRouterProvider (SDK oficial, chat.send_async)
└── features/                 # Módulos verticais autocontidos por capacidade de negócio
    ├── health/               # Feature de monitoramento e liveness check
    │   ├── routes.py
    │   └── schemas.py
    └── modernization/        # Feature principal: pastas por capacidade (AD-15)
        ├── routes.py         # 1 rota = 1 caso de uso (FromDishka[...])
        ├── schemas.py        # request.to_command() / response.from_domain()
        ├── use_cases.py      # ModernizeRoutine, GetModernization (+ Command/Query)
        ├── domain/           # Vocabulário puro: aggregate, IR, relatório, SemanticAnalyzer
        ├── parsing/          # strategy.py (SQLParser) · plpgsql.py (PglastParser)
        ├── generation/       # generate_code.py (GenerateCode) · prompt.py
        ├── evaluation/       # equivalence.py · scenarios.py · repository.py · models.py
        ├── validation/       # validate_code.py (CodeCheck, Rule, ValidateCode)
        │                     # python_ast_check.py · ruff_check.py
        ├── persistence/      # repository.py (port + SQLAlchemy) · execution_log.py
        │                     # models.py (ORM) · mapper.py
        └── graph/            # builder.py (graph + run_modernization) · nodes.py · state.py
migrations/                   # Alembic (env async + versions/)
examples/                     # Anexo A (schema.sql), Anexos B–F (procedures/) e results/
scripts/run_examples.py       # Gera, avalia e grava results/
docker-compose.langfuse.yml   # Stack local opcional de observabilidade
tests/{unit,integration,fixtures/procedures}
docker/postgres/init/         # Cria bancos de testes e avaliação
```

---

## Decisões de tradução por anexo

Resultados medidos automaticamente por `scripts/run_examples.py`, modelo
`openrouter/z-ai/glm-5.3-flash` e schema do Anexo A. Os módulos são saídas reais do LLM, sem
ajustes manuais. Detalhes em [SUMMARY.md](examples/results/SUMMARY.md) e nos
`evaluation.json`; a [linha de base v2](examples/evaluation/baseline-v2.json) conserva as duas
rodadas v2. Todas as rodadas ficam em `evaluation_results` (`GET /evaluations` mostra a última
de cada rotina).

| rodada | pipeline | rotinas equivalentes | casos | só holdout | o que falhou ou custou tentativa |
|---|---|---|---|---|---|
| v2 (duas rodadas) | AST + Ruff | 1/5 | 8/19 | — | C, D, E, F |
| v3 #1 | AST + Ruff | 4/5 | 14/19 | — | D: bind sem tipo no log de erro |
| v3 #2 | AST + Ruff | 5/5 | 19/19 | — | — |
| v3 #3 | AST + Ruff | 4/5 | 16/19 | — | F: `CAST(:x AS INTERVAL)` com `str`, fallback engoliu o erro |
| v3 #4 | + comportamento no loop (AD-17) | 4/5 | 14/21 | 4/5 · 6/9 | D: retentativa voltou com cerca markdown no código |
| v3 #5 | idem | 5/5 | 21/21 | 5/5 · 9/9 | D (bind sem tipo) e F (`python` solto no topo) corrigidos na 2ª tentativa |
| **v4 #1** (atual em `examples/results/`) | + cerca removida, decisões tolerantes (AD-05) | **5/5** | **21/21** | **5/5 · 9/9** | F: fallback sem alias de coluna, corrigido na 2ª tentativa |

O mesmo prompt v3, com temperatura 0, deu 14, 19 e 16 de 19: a melhora sobre a v2 é
consistente, mas uma rodada isolada não garante a tradução. O que mudou o resultado a partir
da #4 foi o check de comportamento no loop (AD-17):

- **#4:** o loop funcionou para o D (feedback com as divergências dos casos dev, sem nenhum nome
  de caso holdout), mas a 2ª resposta do LLM trouxe uma cerca markdown dentro do
  `python_code` e as 2 tentativas acabaram.
- **#5:** o mesmo bind sem tipo do D foi corrigido pelo feedback de comportamento, e a versão
  corrigida passou também nos 3 casos holdout do D, que o LLM nunca viu. O F perdeu uma
  tentativa com `python` solto na 1ª linha (sobra de cerca, compila e só o Ruff acusa).
- **v4:** a cerca passou a ser removida deterministicamente na geração, e a regra 10 do prompt
  cita o bind de texto no `jsonb_build_object`; o D passou de 1ª. A primeira tentativa de
  rodada v4 abortou: o LLM devolveu uma decisão arquitetural sem `topic`, e o contrato
  rejeitava a resposta inteira. Decisões fora do contrato agora são descartadas com warning
  (AD-05). Na rodada válida, o F gerou um fallback sem alias de coluna (`NoSuchColumnError`
  no caso de período invertido) e o check de comportamento devolveu isso como feedback.

As seções por anexo abaixo descrevem o código da rodada v4 #1.

| anexo | estratégia (recomendada → LLM) | v2 commitada / anterior | v3 #1 | v4 #1 (tentativas) | dev · holdout |
|---|---|---|---|---|---|
| B `fn_saldo_cliente` | `database_delegated` → `database_delegated` | 3/3 / 3/3 | 3/3 | 3/3 (1) | 2/2 · 1/1 |
| C `sp_atualizar_status_contas_inativas` | `hybrid` → `hybrid` | 2/4 / 0/4 | 4/4 | 4/4 (1) | 2/2 · 2/2 |
| D `sp_transferir_entre_contas` | `hybrid` → `hybrid` | 2/7 / 5/7 | 2/7 | 7/7 (1) | 4/4 · 3/3 |
| E `sp_processar_lote_taxas` | `hybrid` → `hybrid` | 1/2 / 0/2 | 2/2 | 3/3 (1) | 2/2 · 1/1 |
| F `sp_relatorio_mensal_cliente` | `hybrid` → `hybrid` | 0/3 / 0/3 | 3/3 | 4/4 (2) | 2/2 · 2/2 |

v2 e v3 #1 rodaram com o dataset de 19 casos; v4 com o de 21 (12 dev, 9 holdout).

### B · `fn_saldo_cliente`

Uma consulta agregada com filtro de contas ativas, toda em SQL; `CAST(... AS NUMERIC(18,2))`
na soma e retorno `Decimal` quantizado. **3/3 casos**: cliente com contas ativas e inativas,
cliente sem contas ativas e cliente inexistente. O módulo declara uma dataclass
`SaldoClienteResult` que não usa (código morto do LLM; Ruff não acusa classe não usada).

### C · `sp_atualizar_status_contas_inativas`

Valida `p_dias` em Python com exceção própria (`None` ou `<= 0`), mantém `UPDATE … NOT EXISTS`
no banco com `CAST(:p_dias AS INT) * INTERVAL '1 day'`, e GET DIAGNOSTICS vira
`result.rowcount`. O OUT vira uma frozen dataclass; os binds da auditoria têm CAST.
**4/4 casos**: 30 dias (inativa a conta parada e a que nunca moveu), 3650 dias, zero e nulo.

### D · `sp_transferir_entre_contas`

O corpo inteiro roda em `async with conn.begin_nested()`, porque o `EXCEPTION WHEN OTHERS`
original cobre também os RAISE de validação. FOR UPDATE nas duas contas e as escritas ficam na
mesma conexão; valores em `Decimal` e `CAST(:valor AS NUMERIC(18,2))`. Cada RAISE virou uma
subclasse de `TransferenciaError`. No handler, a auditoria de erro tipa todos os binds,
inclusive `CAST(:erro AS TEXT)` (o bind que quebrou as rodadas v3 #1, #4 e #5), e o erro é
relançado. **7/7 casos**: transferência normal, saldo integral, saldo insuficiente, mesma conta,
destino inativo, origem inexistente e valor negativo. A ordem dos locks (origem, depois destino)
é a do original, com o mesmo risco de deadlock em transferências cruzadas.

### E · `sp_processar_lote_taxas`

O loop cursor-a-cursor virou **um único statement** com CTEs: taxa vigente por `LEFT JOIN
LATERAL`, cálculo com os dois arredondamentos `NUMERIC(18,2)` do original (após o `GREATEST` e
após o multiplicador), débito agregado por conta antes do `UPDATE … FROM`, e os inserts de
`TARIFA` e de auditoria por transação como CTEs de escrita. Python só grava o log consolidado
do lote (o `logger.info` é um acréscimo do LLM; o original não tem NOTICE). Diferente da v3 #1, o N+1 sumiu de fato: o número de consultas não
depende do tamanho do lote. **3/3 casos**: lote com tarifas repetidas e arredondamento, dia sem
movimentos (ainda gera auditoria) e o lote holdout em outra conta.

### F · `sp_relatorio_mensal_cliente`

CTE recursiva de meses, agregações e a chamada a `fn_saldo_cliente` ficam em SQL; retorna uma
lista de frozen dataclasses com `Decimal` quantizado. A validação de período roda **dentro** do
savepoint, como no original: o WHEN OTHERS captura o período invertido e devolve a linha
degradada depois do rollback do savepoint. A consulta de fallback tem aliases explícitos, que
foi a correção pedida pelo check de comportamento na 2ª tentativa. **4/4 casos**: quatro meses
com mês sem movimento, cliente sem contas ativas, período invertido e outro cliente em três
meses. NOTICE/logging e falhas externas adicionais ficam fora desses casos (AD-16).

---

## Architectural Decisions

### AD-01 · Parsing: pglast com dois níveis de AST (sem regex)

Avaliação: `pglast.parse_plpgsql` (libpg_query, port do compilador PL/pgSQL do próprio PostgreSQL)
**representa bem a parte procedural**: blocos, `DECLARE`, loops (`FOR` query/range/cursor, `WHILE`,
`FOREACH`), `IF/ELSIF/CASE`, `RAISE` (nível, mensagem, condição), `GET DIAGNOSTICS`,
`EXCEPTION WHEN`, cursores, `EXECUTE`, `COMMIT/ROLLBACK`, `RETURN QUERY`. Ele **não** cobre:

1. o cabeçalho (nome, modos IN/OUT/INOUT/TABLE, retorno) → vem de `pglast.parse_sql`
   (`CreateFunctionStmt`);
2. o SQL embutido, que chega como texto → é re-parseado com `parse_sql` e percorrido com um
   `pglast.Visitor` (tabelas, funções, CTE/recursiva, `FOR UPDATE/SHARE`, JOIN, operadores/funções
   JSONB), com nomes de CTE excluídos das tabelas.

`pglast.scan` (tokens do lexer) é usado apenas para separar `alvo := expressão`. Nenhuma regex foi
necessária. O resultado é o IR `ParsedProcedure` (domínio) — objetos do pglast nunca saem do adapter.
Números de linha do PL/pgSQL são relativos ao corpo; o adapter os converte para linhas absolutas do
source.

### AD-02 · Análise semântica é domínio puro, sem port

`SemanticAnalyzer` opera sobre o IR, é determinístico e não faz I/O; não há eixo real de variação,
então não há interface. Detecta features (IN/OUT, variáveis, cursores, loops, transações,
EXCEPTION, RAISE, GET DIAGNOSTICS, FOR UPDATE, JSONB, CTE, CTE recursiva, chamadas a outras
routines, RETURN QUERY, SQL dinâmico, DML, agregação, JOIN), riscos estruturados
(`N_PLUS_ONE`, `DYNAMIC_SQL`, `TRANSACTION_CONTROL`, `SWALLOWED_EXCEPTION`, `ROW_LOCKING`,
`UNPARSED_SQL`, …) e recomenda uma estratégia.

### AD-03 · SQL vs Python: "keep relational logic close to the database"

Estratégia recomendada deterministicamente:

| condição                                            | estratégia                |
|-----------------------------------------------------|---------------------------|
| sem SQL embutido                                    | `PYTHON_REIMPLEMENTATION` |
| só SQL set-based (sem loops/IF/RAISE/cursor/tx/...) | `DATABASE_DELEGATED`      |
| SQL + controle procedural                           | `HYBRID`                  |

O LLM recebe a recomendação e pode divergir justificando em `architectural_decisions`; o relatório
guarda **as duas** (`recommended_strategy` e `strategy`). Regras no prompt: joins, agregados, CTEs,
DML em massa e locking permanecem como SQL parametrizado (`sqlalchemy.text()` + bind params);
Python coordena validação, fluxo, erros e composição; a transação pertence ao chamador.

### AD-04 · LLM atrás de um gateway: providers declarados, rotas em ordem

As features dependem só do port `LLM` (`generate(LLMRequest) -> LLMResponse`); o composition
root entrega o `LLMGateway`, que o implementa. A configuração é única (nenhum nome de perfil
espalhado pelo código), declarada em YAML (`LLM_CONFIG_FILE`, ver `config/llm.example.yml`):

- **providers**: endpoints (OpenRouter, OpenAI ou qualquer API OpenAI-compatible), declarados
  uma vez com credencial (`api_key_env`, nunca a chave no arquivo), timeout, retries e circuit
  breaker. Todas as rotas de um provider compartilham o breaker: uma queda pula todas de uma vez.
- **routes**: modelos num provider, com seus parâmetros (`reasoning_effort`), em ordem de
  prioridade.
- **budget_seconds**: nenhuma rota nova começa depois disso (o `POST /modernize` é síncrono).

O gateway percorre as rotas: uma rota que falha com `IntegrationError` (provider fora, circuito
aberto, timeout, request rejeitada) passa a vez para a próxima; bug propaga sem tentar outra;
resposta fora do contrato é problema do chamador (loop de reparo, AD-13). Quem respondeu e quem
falhou antes vão para o relatório (`report.generation.provider/model` e um warning por rota que
falhou). Se todas falham, um único `IntegrationError` com `attempts` e `skipped_routes` no
payload (503 + `Retry-After` quando todas estavam com circuito aberto). Sem `LLM_CONFIG_FILE`,
as variáveis `LLM_*` descrevem uma única rota.

`LLMRequest`/`LLMResponse` são modelos próprios (provider, model, tokens, latência,
`finish_reason`, rotas que falharam). `OpenAIProvider` usa o SDK OpenAI e aceita `base_url`
para endpoints compatíveis. `OpenRouterProvider` usa o [SDK oficial OpenRouter](https://openrouter.ai/docs/client-sdks/python/overview),
com `chat.send_async` (uma chave → Claude, Gemini, GPT, Llama só trocando o modelo da rota).
Tudo vive em `app/shared/integrations/llm/` para ser reutilizado por qualquer feature. Cada
provider recebe um `Integration` (AD-14), que traduz qualquer erro do SDK em `IntegrationError` com mensagem
própria (o texto do SDK fica só no `__cause__`/log). O breaker e os retries usam só o flag
`transient` (transporte, timeout, HTTP 408/409/429 e 5xx); erros do chamador (400, 401, 422:
prompt grande demais, chave inválida) não são retentados nem abrem o circuito. Resposta fora
do contrato do LLM vira `IntegrationError` com a causa (e, se `finish_reason=length`, a dica
de `LLM_MAX_OUTPUT_TOKENS`/`LLM_REASONING_EFFORT`).
Os providers são construídos por `app/shared/integrations/llm/registry.py` (tipo → adapter),
chamado só pelo composition root; um provider usado sem chave derruba o startup. Adicionar
`AnthropicProvider`/`GeminiProvider` = um pacote novo + uma entrada no registry; nodes e
services não mudam. Outra estratégia de roteamento (peso, custo, latência) seria uma política
plugada no gateway; hoje a ordem é a da lista.

O `Integration` de cada provider declarado tem um `CircuitBreaker` (genérico, em
`app/shared/resilience/`): após N falhas transitórias consecutivas (cada uma já com os retries
esgotados) o circuito abre e as chamadas falham na hora com `IntegrationError(retry_after=…)`,
e o gateway passa para a próxima rota; depois do reset, uma única chamada de teste decide se
fecha ou reabre.

OpenAI faz retries pelo SDK (`Integration(retries=0)`). O SDK OpenRouter limita retries por
tempo, sem limite de tentativas; por isso o adapter desativa os retries internos e o
`Integration` faz `max_retries` tentativas adicionais para falhas transitórias. O circuito
conta uma falha somente após essas tentativas se esgotarem. URL, título do app e timeout
são configurados no SDK OpenRouter (`server_url`, `x_open_router_title`, `timeout_ms`).

Modelos de raciocínio contam os tokens de "pensamento" dentro de `max_completion_tokens`. Medido
com `z-ai/glm-5.3-flash` no Anexo F: sem limite, gastou >32k tokens pensando e devolveu conteúdo
vazio (`finish_reason=length`, ~180 s); com `LLM_REASONING_EFFORT=low`, ~2k tokens e ~11 s, com a
resposta completa. Por isso o esforço é configurável (enviado como `reasoning_effort`, que
OpenAI e OpenRouter aceitam); respostas válidas mantêm o `finish_reason` no relatório. Se o modelo omitir `strategy` no contrato, usa-se a
estratégia recomendada deterministicamente e registra-se um warning: o código é o produto, a
estratégia é metadado.

### AD-05 · Prompt construído a partir da análise, não da procedure bruta

`GenerationPromptBuilder` monta: assinatura e parâmetros, declarações, **outline do fluxo de
controle** (árvore do IR com linhas), SQL embutido com fatos extraídos, features/riscos/
recomendações/estratégia, schema opcional e, por último, o source original "apenas como
referência". A resposta é um contrato JSON validado com Pydantic, tolerante a desvios que
não afetam o código: JSON dentro de prosa ou cercas Markdown; cerca Markdown deixada **dentro**
de `python_code` (```` ```python ````, `python` solto na 1ª linha, ```` ``` ```` colado na
última), removida com warning; decisão arquitetural fora do contrato, descartada com warning.
Os três casos foram observados com o modelo e cada um custava uma tentativa do loop ou
abortava a execução. Código ausente ou vazio continua sendo `IntegrationError`.
`PROMPT_VERSION` vai para o relatório.

### AD-06 · Validação: checks são strategies, a política está nas rules

Cada check (`CodeCheck`) só reporta achados. `PythonASTCheck` usa `ast.parse`. `RuffCheck` executa
o binário do Ruff isolado (`--isolated`, stdin, regras de correção `E4,E7,E9,F,B,ASYNC,S608` —
`S608` pega SQL montado com f-string) com `subprocess.run` numa worker thread: funciona em
qualquer event loop (o `SelectorEventLoop` do Windows, usado pelo `langgraph dev`, não suporta
subprocess async). Se um achado **bloqueia** (`failure`) ou só rebaixa para `partial` é política,
declarada uma vez em `core/providers.py`: `Rule(PythonASTCheck(), blocking=True)`,
`Rule(RuffCheck(...), blocking=False)`, `Rule(BehaviorCheck(...), blocking=False)` (AD-17).
Cada check recebe o código e a rotina original (`Routine`); os estáticos ignoram a rotina, e
um check que não tem como rodar responde `Skipped(motivo)`, que passa e vira warning no
relatório. `ValidateCode` roda os checks em paralelo e propaga
erros de execução da própria ferramenta. Sintaxe inválida continua sendo um resultado bloqueante
que pode provocar reparo. Novo check (mypy, bandit, execução contra banco de teste): um módulo em
`validation/` + uma `Rule`.

### AD-07 · Toda execução é persistida, dentro do graph

`record_start` grava `running` e commita antes da chamada ao LLM; `record_result` grava a
conclusão normal. Se um passo lança exceção, `_tracked` (o wrapper de cada node) grava
`failure` com tudo o que foi produzido até ali (`ExecutionLog.fail`, transação curta) e relança
a exceção, sem engoli-la. A persistência fica **no graph** porque ele tem mais de uma porta de
entrada: `POST /modernize` e a API/Studio do servidor LangGraph, que não passa pelos handlers
FastAPI. Nas rotas FastAPI, `PipelineProgress` só leva o `execution_id` até o handler global,
que o devolve na resposta de erro. Falha nos próprios nós de persistência não é gravada:
uma run que não pode ser registrada deve falhar alto.

### AD-08 · Transação aberta pelo caso de uso, repositórios injetados

O caso de uso declara **onde** a transação começa e termina; os repositórios são injetados como
qualquer outra dependência e trabalham dentro da transação em andamento:

```python
async with self._transactions.transaction() as tx:
    running = await self._modernizations.get(execution_id)  # NotFoundError se não existe
    await self._modernizations.update(running.complete(outcome))
    await tx.commit()  # explícito: sair sem commit() faz rollback
```

- **Port** genérico em `app/shared/persistence.py` (`TransactionManager`, `Transaction`), sem
  SQLAlchemy: nenhuma feature precisa de uma classe de transação própria.
- **Mecânica** em `app/core/database/transaction.py` (`SessionTransactionManager`): `transaction()`
  abre uma `AsyncSession` e a torna a sessão *corrente* da task asyncio (`ContextVar`: requests
  concorrentes nunca compartilham sessão); ao sair, descarta o que não foi commitado e fecha.
  Transação aninhada e uso de repositório fora de transação falham alto (testado).
- **Repositório** da feature (`SqlAlchemyModernizationRepository`) recebe o manager e usa
  `current_session()`: só faz `flush`, nunca `commit`.

Commit é explícito por escolha: nada é gravado por acidente. Uma run usa duas transações curtas
(AD-07), então nenhuma conexão fica presa durante a chamada ao LLM. Ler, alterar e gravar fica na
mesma transação, e vários repositórios podem participar da mesma. Adicionar `evaluation_results` ou
`llm_calls`: novo model em `app/features/modernization/persistence/models.py`, novo
repository, novo mapper, um `provide` em `app/core/providers.py` e uma migration — nenhum módulo
existente precisa mudar. O port do repositório expressa necessidades do domínio (`save`,
`update`, `get`), não um CRUD genérico.

### AD-09 · O graph é o fluxo da feature, sem port de pipeline

Havia um `ModernizationPipeline` (Protocol) implementado por `LangGraphModernizationPipeline`.
Foram removidos: só existia uma implementação e nenhum teste usava outra (os testes rodam o graph
real, com fakes só nas bordas: LLM e banco). Um port sem segunda implementação só adiciona
indireção. `ModernizeRoutine` chama `run_modernization(graph, ...)` (`graph/builder.py`), que
também é o único lugar que conhece o canal `progress` lido pelo wrapper dos nós. Trocar de
orquestrador muda `graph/` e o caso de uso; steps, strategies e `ExecutionLog` não conhecem
LangGraph (garantido por teste).

### AD-10 · Pydantic no domínio

Modelos de domínio são `ValueObject` (Pydantic `frozen`, `extra="forbid"`): imutáveis, validados
e serializáveis para JSONB sem camada extra. Pydantic é uma lib de modelagem, não um vendor de
infraestrutura; o trade-off é aceitar essa dependência no núcleo em troca de muito menos código
de serialização.

### AD-11 · Composition root com dishka

`app/core/providers.py` é o único módulo que importa adapters concretos. Cada dependência é
declarada uma vez como provider [dishka](https://dishka.readthedocs.io/) (`@provide`, escopo
`APP`): `InfrastructureProvider` (Settings, engine com `dispose` na finalização,
`TransactionManager`, `LLM` = gateway) e `ModernizationProvider` (repositório, `ExecutionLog`,
steps, regras de validação, graph, casos de uso). Os construtores continuam explícitos;
o dishka só resolve o grafo de dependências, valida-o ao criar o container (dependência
faltando quebra no boot) e finaliza recursos no `close()`. `build_container(settings,
*overrides)` cria o container; `default_container()` (`@cache`) guarda-o **por processo**.

Antes, cada feature tinha um `api/dependencies.py` lendo um `Container` com um campo por
service, e a feature importava o `core.bootstrap` (ciclo core ↔ feature). Agora a rota só
declara o tipo: `use_case: FromDishka[ModernizeRoutine]` com
`APIRouter(route_class=DishkaRoute)`; um caso de uso novo custa um `provide` em `providers.py`.
`test_architecture.py` proíbe features de importar `providers`/`bootstrap`/`server` e restringe
o dishka ao core e às rotas.

O `langgraph dev` serve dois pontos de entrada no mesmo processo — o lifespan da API FastAPI e a
factory `make_graph()` do `langgraph.json` — e ambos usam `default_container()`: um engine (pool de
conexões), um graph, uma leitura de `Settings`. Verificado no container: após uma execução por
`/modernize` e outra por `/runs/wait`, o app mantém **1** conexão no Postgres (eram 2 pools).
Detalhe necessário: o `langgraph.json` referencia módulos (`app.core.bootstrap:make_graph`), não
arquivos (`./app/core/bootstrap.py:...`) — por arquivo, o servidor executa o módulo de novo com outro
nome, e o cache (e o engine) duplicaria.

A entrega é que varia por ponto de entrada: `FromDishka[T]` nas rotas, `async def make_graph()`
no `langgraph.json` (o servidor LangGraph aguarda factories assíncronas), construtor nos
services/nodes. O lifespan resolve o caso de uso no startup (chave de LLM ausente falha o boot,
não a primeira request) e fecha o container no shutdown. Testes passam
`create_app(build_container(settings, fakes))`, em que `fakes` é um provider que sobrescreve os
tipos que precisam (o último provider vence); scripts usam `build_container` direto.

### AD-12 · Docker

Imagem `python:3.14-slim` + `uv sync --frozen --no-dev` (lockfile), usuário não-root. Migrations
rodam num serviço one-shot (`migrate`), e o `app` depende de
`service_completed_successfully` — a app nunca sobe com schema desatualizado, e múltiplas réplicas
não competem pela migration.

**Servidor: `langgraph dev`, não `uvicorn` nem `langgraph up`.** O desafio pede o pipeline exposto
por um servidor local do LangGraph CLI. O `langgraph dev` (runtime em memória) serve o graph, a API
nativa (`/runs`, `/threads`), o Studio e, via `http.app`, as rotas `/health` e `/modernize` num
processo só. O `langgraph up`/`langgraph build` gera a imagem de produção, mas exige Redis +
Postgres próprios do runtime e uma chave LangSmith/licença — dependência externa que prejudica a
reprodutibilidade de um teste local. Trade-offs do `dev` aceitos aqui: fila e checkpoints em memória
(se perdem no restart — o que importa, `modernization_history`, está no nosso Postgres) e um
worker. Em produção: imagem do `langgraph build` com Redis/Postgres, ou as rotas FastAPI em
`uvicorn`/`gunicorn` com N réplicas (o graph é o mesmo). Sem a diretiva `# syntax=` no Dockerfile:
o frontend embutido já suporta `RUN --mount=type=cache`, e a imagem externa do frontend falhou de
forma intermitente (`frontend grpc server closed unexpectedly`).

### AD-13 · Loop de reparo limitado (`validation → generation`)

Quando a validação reprova (AST bloqueante, Ruff **ou** divergência de comportamento nos casos
dev, AD-17), a geração roda de novo com
`RepairFeedback` (código anterior + lista de problemas) no prompt. Observado nos anexos: em execuções
diferentes, C e D foram reprovados na 1ª tentativa (Ruff) e aprovados na 2ª — ver
`examples/results/SUMMARY.md`, coluna "tentativas". Limites, porque o `POST /modernize` é
síncrono e cada tentativa é outra chamada ao LLM:

- `GENERATION_MAX_ATTEMPTS=2` (uma retentativa): cobre os erros pontuais observados; pior caso
  ≈ 2× a latência de geração.
- `GENERATION_RETRY_BUDGET_SECONDS=90`: não inicia retentativa em run que já passou do orçamento
  (uma chamada lenta de 140 s ao provider não vira 280 s de espera).
- Retentativa que falha na geração (ex.: JSON inválido) não apaga a anterior: o graph registra
  `failure` preservando o código da tentativa anterior no relatório.

Trade-off: retentar também por lint (não bloqueante) custa uma chamada a mais em troca de código
limpo; `GENERATION_MAX_ATTEMPTS=1` desliga o loop. Para requests que não podem esperar, o caminho é
assíncrono (ver Evolução futura).

### AD-14 · Erros: uma classe por papel, mensagem + payload

Não há uma classe por caso de erro. `app/shared/errors.py` define `AppError` (falha nossa, 500),
`DomainError` (a entrada viola uma regra, 400) e `NotFoundError` (404);
`app/shared/integrations/errors.py` define `IntegrationError` (dependência falhou: 502, 503 se
`retry_after`, 504 se `timeout`). A classe diz **quem é o culpado** e o handler deriva o status
dela; a **mensagem** diz o que aconteceu; o **payload** (`**kwargs` JSON) leva dados públicos
para o cliente e para `report.errors[].payload`:

```python
raise DomainError(f"Only LANGUAGE plpgsql is supported (got {language!r})", language=language)
raise NotFoundError(f"Modernization {id} not found", execution_id=str(id))
raise self._integration.error("returned no choices")  # dentro de um adapter
```

Mensagem e payload são públicos: nada de texto de SDK, segredos ou stack (isso fica no
`__cause__`, que é logado). Erros de vendor nunca sobem crus: o `Integration`, instanciado uma
vez por dependência, os traduz de forma genérica (status HTTP exposto pelo SDK, ou timeout /
transporte na cadeia de causas), então o core não importa nenhum SDK. `test_architecture.py`
fixa quem pode criar cada classe: `IntegrationError` só em `shared/integrations` e em
`generation/` (que valida a resposta do LLM); `NotFoundError` só em `persistence/`.

### AD-15 · Pastas por capacidade, contrato por papel, abstração só com variação real

A feature começou em camadas técnicas (`api/`, `application/ports/…`, `infrastructure/…`, 21
pastas): uma capacidade como validação ficava em três lugares (port, composite, adapters), e havia
port para tudo. Hoje são 7 pastas por capacidade (`domain`, `parsing`, `generation`, `validation`,
`persistence`, `evaluation`, `graph`) mais `routes.py`, `schemas.py` e `use_cases.py`:

- **Contrato junto de quem o consome**: `CodeCheck` ao lado de `ValidateCode`,
  `ModernizationRepository` ao lado da implementação SQLAlchemy, `SQLParser` no pacote de parsing.
  Sem pasta `ports/` genérica.
- **Port × strategy**: port é uma fronteira com sistema externo, trocada por fake nos testes
  (`LLM`, `TransactionManager`, `ModernizationRepository`); strategy é um algoritmo intercambiável
  escolhido ou combinado pela aplicação (`SQLParser` por dialeto, `CodeCheck` por regra). Sem
  segunda implementação nem fake, não há abstração (AD-09).
- **Contrato por papel** ([tabela](#contratos-por-papel)): rota de 3 linhas, caso de uso com um
  `execute`, nó `StepNode` fino com `require()`. Regra de negócio fica no domínio
  (`Modernization.fail`, `PipelineError.from_exception`) ou no `ExecutionLog`, nunca no graph.
- **Fronteira por arquivo, garantida por teste**: sem pasta `infrastructure/`,
  `test_architecture.py` fixa quem importa cada lib (`pglast` só em `parsing/plpgsql.py`, `ruff`
  só em `validation/ruff_check.py`, `sqlalchemy` nos módulos de persistência/avaliação, `langgraph` só
  em `graph/builder.py`); domínio, casos de uso, steps e contratos não importam vendor, core nem
  DI; rotas só conhecem casos de uso, schemas e domínio.
- **Sem `__init__.py`** (namespace packages): imports apontam para o módulo; a estrutura rasa
  mantém esses caminhos curtos.

Crescimento: novo dialeto = `parsing/<dialeto>.py`; novo check = `validation/<check>.py` + uma
`Rule`; novo caso de uso = uma classe em `use_cases.py` + um `provide`; nova tabela =
model/mapper/repository em `persistence/` + migration.

### AD-16 · Equivalência comportamental medida, separada da validade estática

A métrica `behavioral_equivalence` executa a rotina PL/pgSQL original e o módulo gerado
com os mesmos dados e entradas. Para cada um dos **21 casos B–F**, cria um schema descartável
com o Anexo A, seed, dependências e original no banco `modernizer_eval`. Cada lado roda em
transação revertida; no fim o schema é removido. Python entra por
`async def <nome_da_rotina>(conn, ...)`, com argumentos convertidos pelos tipos IN/INOUT.

O caso passa quando resultado e estado das cinco tabelas (`clientes`, `contas`, `transacoes`,
`taxas`, `log_auditoria`) coincidem. Resultados preservam ordem e colunas; estado de tabela
ignora ordem. Números são normalizados com `Decimal`. Em procedures sem OUT, o retorno
Python não é comparado. Se ambos lançam erro, Python precisa lançar uma classe definida
no próprio módulo; erro de import/driver ou `TypeError` falham. Nesse caminho, mensagens,
tipos exatos e estado antes do erro não são comparados.

**Limites:** só as entradas do dataset, sem prova geral; NOTICE e logs não são comparados.
O dataset ignora globalmente `id`, `data_transacao` e `criado_em` para tolerar sequências e
relógio; pode ocultar diferenças nessas colunas. Não mede concorrência, contenção, custo das
queries ou dependências externas. O módulo gerado roda num subprocesso
(`evaluation/runner.py`) contra o banco descartável: um crash, um travamento ou estado de módulo
vazado não atingem o servidor. Ainda não é um sandbox (mesma máquina, com rede): em produção,
container sem rede e papel de banco restrito.

Taxa principal = rotinas com todos os casos aprovados / rotinas avaliadas; taxa por caso
expõe progresso parcial. Validade AST e conclusão são indicadores separados. Persistência
em `evaluation_results` inclui prompt/modelo e detalhes (cada caso marca se é holdout). A
avaliação em si não altera o status da modernização; quem altera é o check de comportamento
dentro do pipeline (AD-17), que roda só os casos dev.
O script usa os mesmos casos de uso dos endpoints. `DISTINCT ON` consulta a última avaliação,
preservando as anteriores no histórico.

A linha de base em [`baseline-v2.json`](examples/evaluation/baseline-v2.json) reproduziu
**1/5 rotinas (20%) e 8/19 casos (42,1%)** nas duas rodadas v2, inclusive import inválido do C
e fallback do F com período invertido. `generation-v3` acrescenta CAST em binds ambíguos,
import correto de `AsyncConnection`, savepoints com `async with begin_nested()`,
`Decimal`/arredondamento em cada atribuição NUMERIC e agregação antes de `UPDATE … FROM`.
`generation-v4` só reforça a regra 10 com o bind de texto no `jsonb_build_object`, que falhou em
três rodadas v3.
Os módulos B–F são publicados como saíram do LLM, sem correção manual.

Evolução: versionar dataset/seed; ampliar bordas e concorrência; comparar SQLSTATE e estado
após falhas; restringir colunas ignoradas por tabela; gerar o dataset automaticamente para
qualquer rotina (ver [Evolução futura](#evolução-futura)). Cada caso novo primeiro reproduz o
original.

### AD-17 · Comportamento no loop de reparo, com casos holdout

O mesmo harness do AD-16 roda **dentro do pipeline** como mais um check de validação
(`validation/behavior_check.py`, regra não bloqueante): uma divergência vira achado
(`[behavior] case '...': resultado difere: original ... vs gerado ...`), e o loop do AD-13
regenera com esse feedback. É o que pega o que AST e Ruff não pegam: bind sem tipo no asyncpg,
`begin_nested()` sem `await`, arredondamento, `UPDATE … FROM`, fallback engolindo erro.

**Holdout, para a métrica continuar honesta.** Se os mesmos casos que medem também viram
feedback, o LLM itera até passar neles e o número deixa de medir generalização. Por isso o
dataset separa os casos: **12 dev** (rodam no pipeline e viram feedback) e **9 holdout**
(`holdout: true`, nunca chegam ao prompt). Cada rotina tem os dois tipos, e o holdout repete as
mesmas armadilhas com outros dados (ex.: o E tem um segundo lote, em outra conta, com
arredondamento duplo e duas tarifas). A métrica publica as duas taxas; a de holdout é a que
indica se a correção generaliza. Teste de integração cobre o ciclo: um B errado é regenerado
com o feedback dos casos dev e o prompt nunca contém o nome de um caso holdout.

Escolhas:

- **Não bloqueante:** divergência persistente termina em `partial`, não `failure`. O código é
  Python válido; o relatório diz qual caso diverge. Bloquear faria `valid_python` mentir.
- **Sem cenário, sem verificação:** rotinas fora do dataset (qualquer procedure nova) recebem
  `Skipped` e o warning `[behavior] not run: no evaluation scenario for routine ...`. O status
  não finge que o comportamento foi verificado. Sem `EVALUATION_DATABASE_URL`, idem.
- **Custo:** cada validação de rotina com cenário sobe um subprocesso e roda os casos dev
  (segundos), menos que uma chamada ao LLM. O orçamento do AD-13 continua valendo: uma geração
  lenta pode esgotá-lo antes da retentativa.

---

## Limitações conhecidas

- **Só `LANGUAGE plpgsql`**; funções `LANGUAGE sql` são rejeitadas com `DomainError` (400).
  Arquivos com vários `CREATE FUNCTION` usam o primeiro.
- **Sem catálogo**: tipos `%TYPE`/`%ROWTYPE` não são resolvidos. Parâmetros `%TYPE` são
  reescritos com um placeholder antes da compilação, preservando o tipo original no IR e
  emitindo um warning. SQL inválido vira `DomainError` (400).
- **Builtins vs routines do usuário** é heurística (lista de funções conhecidas + `pg_catalog.`);
  sem acesso ao banco não dá para ter certeza.
- **SQL dinâmico** (`EXECUTE`) não é analisável estaticamente: vira risco `DYNAMIC_SQL`.
- **Comportamento só é verificado onde há dataset**: para os anexos B–F o pipeline compara
  com o original nos casos dev (AD-17); para qualquer outra rotina a validação é só estática
  (AST + Ruff) e o relatório diz que o comportamento não foi verificado. `success` em B–F
  significa Python válido, sem lint e equivalente nos casos dev; `equivalent` na métrica vale
  para os casos do dataset, sem garantia para entradas não cobertas (AD-16).
- `POST /modernize` é síncrono (a request espera o LLM, incluindo a eventual retentativa — AD-13).

## Trade-offs

| escolha                                   | ganho                                   | custo                                      |
|-------------------------------------------|-----------------------------------------|--------------------------------------------|
| LLM gera, validadores determinísticos checam | tradução de casos que regras não cobrem | não determinismo; exige relatório/validação |
| OpenRouter via SDK oficial                | muitos modelos, contrato próprio do SDK | hop extra, markup; `json_object` depende do modelo |
| Duas transações por execução              | rastreabilidade mesmo com crash         | estado `running` intermediário visível     |
| Pydantic no domínio                       | serialização/validação grátis           | dependência de lib no núcleo               |
| Ruff como subprocess                      | isolamento, mesma versão do projeto     | custo de processo por validação (~ms)      |
| API síncrona                              | simples de explicar e testar            | request longa; ver Escalabilidade (filas)  |

---

## Evolução futura

- `AnthropicProvider`/`GeminiProvider` nativos (uma entrada no registry); políticas de roteamento por custo/latência no `LLMGateway`.
- **Dataset de avaliação gerado automaticamente (segunda implementação no pipeline).** Hoje o
  check de comportamento só roda onde há cenário escrito à mão (B–F). Um nó novo, antes da
  geração, montaria esse cenário para qualquer rotina:
  1. banco de teste a partir do `schema_context` enviado na request (o Anexo A, no desafio);
  2. seed e entradas geradas a partir do IR (tipos dos parâmetros, tabelas, colunas e
     constantes usadas em `WHERE`/`CASE`/`IF`, que viram valores de borda) — por LLM ou por
     fuzzing guiado pelos tipos;
  3. cada caso é validado executando a **rotina original** (o oráculo é ela mesma: não há
     rótulo manual; casos que não exercitam nada, ou que dependem de relógio sem controle, são
     descartados);
  4. divisão dev/holdout e o mesmo harness do AD-16/AD-17.

  Riscos a medir: dados gerados que não cobrem os ramos (medir cobertura dos ramos do PL/pgSQL,
  ex. com `plpgsql_check`), viés do mesmo LLM gerando código e dados (usar outro modelo ou
  fuzzing para os dados), e rotinas com efeitos externos (e-mail, `dblink`), que ficam fora.
- Versionar dataset/seed e comparar modelos/prompts por rodada.
- Ampliar bordas, concorrência e falhas intermediárias; isolar Python gerado em container sem
  rede externa e com papel de banco limitado. Langfuse já registra prompts, respostas e tokens.
- Resolver `%TYPE`/`%ROWTYPE` e builtins consultando o catálogo quando houver conexão disponível.
- Métricas de avaliação por modelo (taxa de sucesso, lint, custo, latência) para comparar LLMs.
- Fila, cache de resultado e backpressure por provider: ver [Escalabilidade](#escalabilidade).
- Novos dialetos (T-SQL, PL/SQL) implementando `SQLParser` sobre o mesmo IR.
