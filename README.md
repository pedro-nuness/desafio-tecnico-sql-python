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
- [Endpoints](#endpoints)
- [Relatório](#relatório)
- [Configuração](#configuração)
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

Saída em `examples/results/`: `<procedure>/generated.py`, `<procedure>/report.json` e
[`SUMMARY.md`](examples/results/SUMMARY.md) (status, tentativas, estratégia, riscos, Ruff, tokens,
duração).
Os resultados versionados são uma execução real, **não curada**: falhas e `partial` ficam como
saíram, porque mostram o que a validação pega.

---

## Arquitetura

Ports & Adapters (hexagonal) com Dependency Inversion. Domínio e casos de uso não conhecem
OpenAI, SQLAlchemy, asyncpg, pglast, Ruff nem LangGraph.

```mermaid
flowchart LR
    API --> ModernizationService
    ModernizationService --> ModernizationPipeline
    ModernizationPipeline -. implementado por .-> LangGraph
    LangGraph --> RecordStart
    RecordStart --> Parsing
    Parsing --> Analysis
    Analysis --> Generation
    Generation --> Validation
    Validation -. retry .-> Generation
    Validation --> RecordResult

    Parsing --> SQLParser
    Analysis --> SemanticAnalyzer
    Generation --> CodeGenerationService
    CodeGenerationService --> GenerationPromptBuilder
    CodeGenerationService --> LLMProvider
    Validation --> CodeValidator

    RecordStart --> UnitOfWork
    RecordResult --> UnitOfWork
    ModernizationService -. leitura .-> UnitOfWork
    UnitOfWork --> Repository
    Repository --> PostgreSQL
```

### Ports & Adapters

```mermaid
flowchart TB
    subgraph Driving["Driving adapters"]
        FASTAPI["FastAPI routes<br/>app/core/server.py · app/features"]
        LGCLI["LangGraph server / Studio<br/>langgraph.json"]
    end

    subgraph Core["Application + Domain (sem dependências de vendor)"]
        SVC["ModernizationService<br/>CodeGenerationService"]
        DOM["Domain models · SemanticAnalyzer<br/>ModernizationReport · status rules"]
        subgraph Ports["Ports (typing.Protocol)"]
            P1["SQLParser"]
            P2["LLMProvider"]
            P3["CodeValidator"]
            P4["UnitOfWork / ModernizationRepository"]
            P5["ModernizationPipeline"]
        end
    end

    subgraph Driven["Driven adapters (app/features/modernization/infrastructure, graph)"]
        A1["PglastParser"]
        A2["OpenAIProvider / OpenRouterProvider (SDKs oficiais)"]
        A3["PythonASTValidator · RuffValidator<br/>CompositeCodeValidator"]
        A4["SqlAlchemyUnitOfWork<br/>SqlAlchemyModernizationRepository"]
        A5["LangGraphModernizationPipeline"]
    end

    FASTAPI --> SVC
    LGCLI --> A5
    A5 --> P4
    SVC --> DOM
    SVC --> P4
    SVC --> P5
    SVC --> P2
    A1 -. implements .-> P1
    A2 -. implements .-> P2
    A3 -. implements .-> P3
    A4 -. implements .-> P4
    A5 -. implements .-> P5
    A5 --> P1
    A5 --> P3
```

Direção das dependências (verificada por teste — `tests/unit/test_architecture.py`):

```
shared (reutilizável, zero dependências de core ou features)
core (bootstrap, server, config, database) ──► features (rotas, domínio, ports)
features/modernization:
  api ──► application ──► ports ◄── infrastructure
               │                          │
               └──────► domain ◄──────────┘
  graph (LangGraph) ──► application/ports + domain
app/core/bootstrap.py = composition root (único lugar que conhece todos os adapters)
```

### Eixos de variação (onde existem abstrações — e só lá)

| Eixo                 | Port                                   | Adapters hoje                                         |
|----------------------|----------------------------------------|-------------------------------------------------------|
| LLM provider/modelo  | `LLMProvider`                          | `OpenAIProvider` / `OpenRouterProvider` (SDKs oficiais) |
| Parser SQL           | `SQLParser`                            | `PglastParser`                                        |
| Validadores          | `CodeValidator`                        | `PythonASTValidator`, `RuffValidator`, `CompositeCodeValidator` |
| Persistência         | `UnitOfWork` + `ModernizationRepository` | `SqlAlchemyUnitOfWork`, `SqlAlchemyModernizationRepository` |
| Orquestração         | `ModernizationPipeline`                | `LangGraphModernizationPipeline`                      |

Não há `BaseService`, `BaseRepository`, `GenericDAO` etc. O único "base" é
`ValueObject` (configuração Pydantic `frozen`) e o `DeclarativeBase` do SQLAlchemy.

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
- **Nodes finos** (`app/features/modernization/graph/nodes/`): cada um chama um port/serviço e
  devolve `StateUpdate`; exceções propagam sem tratamento HTTP local.
- **Persistência**: `record_start` commita `running` antes do LLM; `record_result` grava a
  conclusão normal. Nas rotas FastAPI, o handler global grava `failure` com o progresso
  já realizado antes de devolver o erro HTTP e o `execution_id` para consulta.
- **Loop de reparo** (`validation → generation`, AD-13): se a validação reprova (AST ou Ruff), a
  geração roda de novo com o código anterior e a lista de problemas no prompt, até
  `GENERATION_MAX_ATTEMPTS` (default 2) e só se a run tiver menos de
  `GENERATION_RETRY_BUDGET_SECONDS` (default 90 s). `completed_steps` mostra cada tentativa
  (`…generation, validation, generation, validation`) e `report.generation.attempt` diz qual
  tentativa produziu o código final.
- **Dependências injetadas nos nodes** (instâncias callable construídas no builder), não via
  `config`/`context` — assim o mesmo graph roda no uvicorn e no servidor LangGraph.
- **Tratamento global**: não há `try/except` em `app`. O breaker mantém seu controle de
  estados e os retries continuam ativos; ambos observam resultados assíncronos com
  `asyncio.gather(return_exceptions=True)`. O erro final propaga para o handler HTTP.
  `try/finally` permanece para liberar recursos e garantir rollback.

---

## Endpoints

| Método | Rota                         | Descrição                                        |
|--------|------------------------------|--------------------------------------------------|
| GET    | `/health`                    | `{"status": "ok"}`                               |
| POST   | `/modernize`                 | executa o pipeline e persiste a execução         |
| GET    | `/modernizations/{id}`       | recupera uma execução persistida (404 se não existe) |

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

Exceções do pipeline viram erros HTTP: parsing/SQL inválido → 400; falhas de integração
ou resposta gerada inválida → 502; circuito aberto → 503 com `Retry-After`; timeout de
upstream → 504; erro inesperado → 500. O handler grava `failure` e o progresso, incluindo
código já gerado, antes de responder com `detail` e `execution_id`. O relatório fica
disponível no GET da execução. Resultados de validação continuam alimentando o loop de
reparo; erros de execução dos validadores propagam. Entrada inválida → 422.
Se o banco não permitir gravar a falha, o handler registra o erro de persistência e
responde 500 sem anunciar um resultado gravado.

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
| `partial` | código gerado e sintaticamente válido, mas Ruff reportou algo, ou a validação não rodou |
| `failure` | execução interrompida por exceção (progresso preservado) ou Python inválido após reparos |

A regra fica em `PipelineOutcome.status()` (domínio), testada isoladamente.

---

## Configuração

Centralizada em `app/core/config/settings.py` (`pydantic-settings`; lê env vars e `.env`). Nenhum
`os.getenv` espalhado. Copie `.env.example` para `.env` (git-ignored).

| variável                | default                                                       | descrição                                  |
|-------------------------|---------------------------------------------------------------|--------------------------------------------|
| `DATABASE_URL`          | `postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer` | driver async obrigatório          |
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
| `TEST_DATABASE_URL`     | —                                                             | só testes de integração                    |

---

## Migrations

Schema criado **somente** por Alembic (`migrations/versions/20260930_0001_create_modernization_history.py`);
`Base.metadata.create_all` não é usado em lugar nenhum (nem nos testes).
`migrations/env.py` é async e lê a URL do `Settings` (fonte única de configuração).

```bash
uv run alembic upgrade head
```

```bash
uv run alembic revision --autogenerate -m "create evaluation_results"
```

Tabela `modernization_history`: `id UUID` (UUIDv7, ordenável por tempo), `source_code`,
`schema_context`, `generated_code` (nullable), `report JSONB`, `status` (CHECK constraint),
`created_at`/`updated_at` `timestamptz`, índice `(status, created_at)`.

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

- **Unitários (64)**: parser (`pglast`), análise semântica (inclusive sobre IR montado à mão, sem
  parser), `CodeGenerationService` com test double (verifica que o prompt carrega parsing +
  análise; resposta truncada; `strategy` ausente), validadores (AST, Ruff, composite),
  `ModernizationService` com LangGraph real + UoW em memória (sucesso, falha do LLM, falha de
  parsing, Python inválido, lint → partial, crash inesperado), graph (loop de reparo com feedback,
  limite de tentativas, orçamento de tempo, retentativa que falha mantém a anterior, run iniciada
  direto no graph — caminho do servidor LangGraph — é persistida), API (`/health`, `/modernize`, `/modernizations/{id}`), factory de providers e
  **regras de dependência entre camadas** (análise de imports via `ast`).
- **Integração (5)**: Postgres real migrado com Alembic (subprocess), round-trip do repository,
  JSONB, rollback sem commit, e persistência de sucesso **e** falha ponta a ponta.
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
│   ├── bootstrap.py          # Composition root (build_container, make_graph para langgraph.json)
│   ├── config/settings.py    # Configurações centralizadas com Pydantic Settings
│   ├── database/             # SQLAlchemy async engine, session factory e Base declarativa
│   │   ├── base.py
│   │   ├── engine.py
│   │   └── session.py
│   ├── dependencies.py       # FastAPI dependencies transversais (container)
│   ├── exception_handlers.py # Handlers globais de erro HTTP
│   └── server.py             # Setup da aplicação FastAPI e lifespan
├── shared/                   # Código utilitário compartilhado entre múltiplos contextos
│   ├── client.py             # HTTP client base reutilizável
│   ├── domain/value_object.py# Base ValueObject imutável (Pydantic frozen)
│   ├── resilience/           # CircuitBreaker genérico (async, sem dependência de vendor)
│   └── integrations/llm/     # Port LLMProvider, LLMConfig, factory e adapters reutilizáveis
│       ├── llm_provider.py   # LLMProvider (Protocol), LLMRequest/LLMResponse
│       ├── factory.py        # create_llm_provider (match + circuit breaker)
│       ├── openai/           # OpenAIProvider (Chat Completions, qualquer endpoint compatível)
│       └── openrouter/       # OpenRouterProvider (SDK oficial, chat.send_async)
└── features/                 # Módulos verticais autocontidos por capacidade de negócio
    ├── health/               # Feature de monitoramento e liveness check
    │   ├── routes.py
    │   └── schemas.py
    └── modernization/        # Feature principal: modernização de SQL para Python
        ├── api/              # Driving adapter HTTP (rotas, schemas e deps da feature)
        │   ├── dependencies.py
        │   ├── routes.py
        │   └── schemas/
        ├── domain/           # Entidades, agregados, value objects e regras puras
        │   ├── enums.py
        │   ├── exceptions.py
        │   ├── models/       # IR de parsing, análise semântica, geração e validação
        │   └── services/     # Analisador semântico determinístico
        ├── application/      # Casos de uso e portas abstratas (Protocols)
        │   ├── ports/        # Parsing, validação, repositórios e pipeline (LLM vem de shared)
        │   └── services/     # ModernizationService e CodeGenerationService
        ├── graph/            # Workflow LangGraph (pipeline, state, builder e nodes finos)
        │   ├── builder.py
        │   ├── pipeline.py
        │   ├── state.py
        │   └── nodes/
        ├── infrastructure/   # Driven adapters (parsing pglast, Ruff/AST, persistência UoW/Repo)
        │   ├── parsing/      # PglastParser
        │   ├── persistence/  # SqlAlchemyUnitOfWork, repositórios, mappers e models ORM
        │   └── validation/   # PythonASTValidator, RuffValidator e CompositeCodeValidator
        └── prompts/          # Templates de engenharia de prompt (generation_prompt.py)
migrations/                   # Alembic (env async + versions/)
examples/                     # Anexo A (schema.sql), Anexos B–F (procedures/) e results/
scripts/run_examples.py       # Roda os anexos pelo ModernizationService e grava results/
tests/{unit,integration,fixtures/procedures}
docker/postgres/init/         # Cria o banco de testes
```

---

## Decisões de tradução por anexo

Cada anexo exige decisões diferentes. Abaixo: o que o pipeline decidiu (análise determinística +
LLM, registrado em `report.generation.architectural_decisions`), por quê, e o que aconteceu quando o
código gerado foi **executado**.

**Como foi verificado.** A validação do pipeline é estática (AST + Ruff). Para esta seção, o
código de [`examples/results/`](examples/results/SUMMARY.md) foi executado manualmente num banco
descartável com o schema do Anexo A, as cinco rotinas originais instaladas e dados que exercitam
os casos de borda (duas tarifas na mesma conta, valor que expõe arredondamento). Original e
Python rodam com as mesmas entradas, cada um numa transação revertida ao final, e o estado das
tabelas é comparado.

| anexo | estratégia (recomendada → LLM) | validação estática | execução vs original |
|---|---|---|---|
| B `fn_saldo_cliente` | `database_delegated` → `database_delegated` | success | ✅ mesmo valor (mas `float`, não `Decimal`) |
| C `sp_atualizar_status_contas_inativas` | `hybrid` → `hybrid` | success (2ª tentativa) | ❌ erro de runtime: bind param sem tipo |
| D `sp_transferir_entre_contas` | `hybrid` → `hybrid` | success | ❌ `begin_nested()` sem `await`: falha em toda chamada |
| E `sp_processar_lote_taxas` | `hybrid` → `database_delegated` | success | ❌ roda, mas cobra errado (tarifa perdida + arredondamento) |
| F `sp_relatorio_mensal_cliente` | `hybrid` → `hybrid` | success | ❌ query principal falha; fallback falha em seguida |

**Validação estática 5/5, equivalência comportamental 1/5.** É o achado mais importante desta
entrega: `ast.parse` + Ruff provam que o código é Python bem formado, não que ele funciona. Os
erros encontrados se repetem em classes (tipos de bind no asyncpg, transações async, semântica de
`NUMERIC`), então viram regras de prompt e, principalmente, um validador que executa o código
contra um banco de teste — a "evolução desejada" do desafio (ver [Evolução futura](#evolução-futura)).

### B · `fn_saldo_cliente` — função escalar

- **SQL vs Python:** delegado ao SGBD. Uma agregação filtrada é trabalho do banco (usa
  `idx_contas_cliente`); reescrever em Python seria trazer linhas para somar no cliente.
- **Forma:** `async def fn_saldo_cliente(conn: AsyncConnection, p_cliente_id: int)`, SQL em
  `text()` com bind param, transação do chamador. `text()` em vez de SQLAlchemy Core: para uma
  query, o SQL fica lado a lado com o original e é auditável.
- **Verificação:** ✅ mesmo resultado (1500,00). Ressalva: o retorno é `float`; em domínio bancário
  `NUMERIC(18,2)` deve virar `Decimal` (o próprio LLM avisou nos `warnings`). Regra a adicionar
  no prompt.

### C · `sp_atualizar_status_contas_inativas` — IN/OUT, UPDATE em massa, GET DIAGNOSTICS

- **Parâmetro OUT:** vira o retorno, como `@dataclass(frozen=True) ResultadoInativacao(afetadas)`.
  Tupla não tem nome; `dict` não tem tipo; a dataclass é nomeada, imutável e ganha campos sem
  quebrar quem chama.
- **`GET DIAGNOSTICS ROW_COUNT` → `result.rowcount`.** A análise marca `DIAGNOSTICS_SEMANTICS`
  porque a equivalência depende do driver (asyncpg reporta linhas afetadas pelo `UPDATE`).
- **`RAISE EXCEPTION` → `ParametroInvalidoError(ValueError)`**, validado em Python antes de ir ao
  banco (sem round trip para rejeitar entrada inválida). ✅ Mesma mensagem, mesmo estado.
- **UPDATE com `NOT EXISTS`:** permanece set-based em SQL.
- **Verificação:** ❌ para evitar a concatenação `(p_dias || ' days')::INTERVAL`, o LLM usou
  `make_interval(days => :p_dias)`. A intenção é boa, mas o asyncpg usa *prepared statements* e
  o PostgreSQL não infere o tipo desse parâmetro: `could not determine data type of parameter $1`.
  Correção: `make_interval(days => CAST(:p_dias AS int))`. **Lição geral:** com asyncpg, todo bind
  param em posição polimórfica precisa de `CAST` explícito.

### D · `sp_transferir_entre_contas` — transação, `FOR UPDATE`, `EXCEPTION`

- **Transação (a decisão que o anexo pede):** das três opções, o pipeline escolheu a híbrida:
  - validações de entrada (valor, contas iguais) ficam em Python, sem round trip;
  - `SELECT … FOR UPDATE` e as escritas rodam em SQL **na mesma conexão/transação**, o que a
    análise exige pelo risco `ROW_LOCKING`: o lock só vale dentro da transação;
  - a transação é do chamador;
  - um `SAVEPOINT` (`begin_nested`) reproduz a subtransação implícita do bloco `EXCEPTION`.
- **Semântica sutil do original:** o `INSERT` de `TRANSFERENCIA_ERRO` no handler é seguido de
  `RAISE`, que aborta a transação de quem chamou. Ou seja, **no original o log de erro nunca
  persiste**, a menos que o chamador use savepoint. O LLM identificou e preservou esse
  comportamento (está nos `warnings`). Se o objetivo de negócio é auditar falhas, o correto é
  gravar numa transação separada. Essa é uma decisão de produto, não de tradução, e por isso não
  foi tomada automaticamente.
- **Herdado:** a ordem de lock origem→destino permite deadlock entre A→B e B→A simultâneas. A
  correção seria travar por ordem de `id`.
- **Verificação:** ❌ `nested = conn.begin_nested()` sem `await`: no SQLAlchemy async isso cria um
  objeto não iniciado, e o `commit()` lança `AsyncContextNotStarted`. **Toda** transferência
  falha antes de escrever qualquer coisa. AST e Ruff não pegam: o código é válido. Correção:
  `async with conn.begin_nested():`.

### E · `sp_processar_lote_taxas` — cursor, LOOP, CASE, JSONB

- **A decisão central:** o cursor faz 4 SQLs por linha, e a análise marca `N_PLUS_ONE` nos
  quatro. O LLM **divergiu** da recomendação (`hybrid`) e escolheu `database_delegated`: um único
  statement com *data-modifying CTEs* (`UPDATE`/`INSERT … SELECT`) e `LEFT JOIN LATERAL` para a
  taxa vigente. N+1 vira 1 round trip, atômico. A divergência e a justificativa estão no
  relatório (`strategy` ≠ `recommended_strategy`).
- **Alternativas consideradas:**

  | abordagem | round trips | regra de negócio | risco |
  |---|---|---|---|
  | ingênua (loop Python, SQL por linha) | O(n) | visível em Python | performance |
  | *bulk fetch* (1 SELECT transações + 1 taxas, cálculo em Python com `Decimal`, escrita em lote) | 2–4, constante | visível e testável unitariamente | pouco |
  | set-based puro (escolhida pelo LLM) | 1 | escondida no SQL | semântica sutil, como a verificação mostrou |

- **Verificação:** ❌ roda sem erro e **cobra errado**, com duas divergências que passaram na
  validação estática com `success`:
  1. **Tarifa perdida:** `UPDATE contas … FROM base` com duas tarifas para a mesma conta atualiza
     a linha **uma vez só**. No PostgreSQL, quando o `FROM` casa várias linhas com o mesmo alvo,
     só uma é aplicada. Conta 10: original 995,80, gerado 998,00. A tarifa de 2,20 é registrada
     em `transacoes`, mas não é debitada. Correção: agregar antes,
     `FROM (SELECT conta_origem_id, SUM(taxa) … GROUP BY 1)`.
  2. **Arredondamento:** no original, `v_taxa` é `NUMERIC(18,2)`, então arredonda depois do
     `GREATEST` e de novo depois do multiplicador do `CASE`. O gerado multiplica sem arredondar no
     meio. DEPOSITO de 10,10: original 0,03, gerado 0,02. Total do lote 4,23 vs 4,22, e o JSONB
     grava `0.022725`. Correção: `ROUND(ROUND(GREATEST(…), 2) * mult, 2)`.
- **Recomendação para produção:** *bulk fetch* com cálculo em Python (`Decimal`, arredondamento
  explícito, regra testável sem banco), ou o set-based com as duas correções **e** teste de
  equivalência obrigatório.
- **Herdado (avisado pelo LLM):** nenhum lock ou idempotência, então rodar o lote duas vezes para
  a mesma data cobra em dobro. `DATE(data_transacao) = :d` não usa índice; um intervalo de
  timestamps seria equivalente e usaria o índice.

### F · `sp_relatorio_mensal_cliente` — CTE recursiva, função aninhada, SETOF, fallback

- **CTE recursiva:** mantida em SQL (risco `RECURSIVE_CTE`). Gerar os meses em Python é trivial,
  mas `meses LEFT JOIN movimento` é uma unidade relacional; separar exigiria fazer o merge no
  cliente.
- **Função aninhada:** `SELECT fn_saldo_cliente(:id)` continua chamando a função do banco (risco
  `EXTERNAL_ROUTINE_DEPENDENCY`), em vez de importar a tradução do Anexo B. O pipeline processa uma
  rotina por vez e não sabe o que já foi migrado. Evolução: um catálogo de rotinas migradas no
  contexto do prompt, para gerar `from … import fn_saldo_cliente`.
- **SETOF / `RETURN QUERY`** → `list[LinhaRelatorioMensal]` (dataclass frozen). Para volumes
  grandes, `AsyncIterator` com `stream_results`.
- **`RAISE NOTICE`/`WARNING`** → `logger.info`/`logger.warning`.
- **Fallback** (`WHEN OTHERS` → linha degradada) → `except Exception` que executa o `SELECT` de
  fallback.
- **Verificação:** ❌ três problemas:
  1. `DATE_TRUNC('month', :p_data_inicio)` com bind param sem tipo resulta em
     `function date_trunc(unknown, unknown) is not unique`: a query principal **sempre** falha. É
     a mesma classe de erro do C; correção: `CAST(:p_data_inicio AS date)`.
  2. O fallback roda na mesma transação, já abortada pelo erro anterior, e lança
     `current transaction is aborted`. No original funciona porque o bloco `EXCEPTION` do
     PL/pgSQL é um savepoint implícito. Correção: `async with conn.begin_nested():` em volta da
     query principal. O próprio LLM levantou essa dúvida nos `warnings`, mas não aplicou.
  3. **Mudança de comportamento assumida pelo LLM:** período inválido. No original, o
     `RAISE EXCEPTION` está dentro do bloco com `WHEN OTHERS`, então é **capturado** e a função
     devolve a linha de fallback com um `WARNING` (confirmado executando). O gerado lança
     `RelatorioError`. O LLM registrou isso como decisão ("falha intencional"), então aparece no
     relatório, mas a justificativa está errada. Também por leitura do código: `v_saldo_atual`
     não é inicializado antes do `try`, então uma falha em `fn_saldo_cliente` viraria
     `UnboundLocalError` no fallback.
- A análise determinística não marcou `SWALLOWED_EXCEPTION` aqui: bug conhecido, porque
  `RAISE WARNING` é contado como re-raise.

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

### AD-04 · LLM atrás de `LLMProvider`; SDK oficial de cada provider

`LLMRequest`/`LLMResponse` são modelos próprios (provider, model, tokens, latência,
`finish_reason`). `OpenAIProvider` usa o SDK OpenAI e aceita `base_url` para endpoints
compatíveis. `OpenRouterProvider` é independente e usa o [SDK oficial OpenRouter](https://openrouter.ai/docs/client-sdks/python/overview),
com `chat.send_async` (uma chave → Claude, Gemini, GPT, Llama só trocando `LLM_MODEL`). Tudo vive em
`app/shared/integrations/llm/` para ser reutilizado por qualquer feature. Erros dos SDKs propagam
com seu tipo original até `app/core/exception_handlers.py`; violações do contrato do
próprio adapter usam `IntegrationError`. O breaker também conta as exceções nativas
informadas pelo adapter, mantendo erros de programação fora dessa contagem.
A seleção acontece em `app/shared/integrations/llm/factory.py` (um `match`), chamada só pelo
composition root com `Settings.llm_config()`. Adicionar `AnthropicProvider`/`GeminiProvider` =
um pacote novo + um `case`; nodes e services não mudam.

Todo provider sai da factory com um `CircuitBreaker` injetado (breaker genérico em
`app/shared/resilience/`): após N falhas consecutivas (cada uma já com os retries
esgotados) o circuito abre e as chamadas falham na hora com `CircuitOpenError`, que o
handler global converte em HTTP 503; depois do reset,
uma única chamada de teste decide se fecha ou reabre.

OpenAI faz retries pelo SDK. O SDK OpenRouter limita retries por tempo, sem limite de
tentativas; por isso o adapter desativa os retries internos e preserva `LLM_MAX_RETRIES`
com tentativas adicionais para falhas de transporte, HTTP 408/409/429 e 5xx. O circuito
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
referência". A resposta é um contrato JSON validado com Pydantic (tolerante a fences Markdown).
`PROMPT_VERSION` vai para o relatório.

### AD-06 · Validação: composite, bloqueante vs não bloqueante

`PythonASTValidator` (`ast.parse`) é **bloqueante** (falha ⇒ `failure`). `RuffValidator` executa o
binário do Ruff isolado (`--isolated`, stdin, regras de correção `E4,E7,E9,F,B,ASYNC,S608` —
`S608` pega SQL montado com f-string), é **não bloqueante** (achados ⇒ `partial`). O Ruff roda com
`subprocess.run` numa worker thread: funciona em qualquer event loop (o `SelectorEventLoop` do
Windows, usado pelo `langgraph dev`, não suporta subprocess async). `CompositeCodeValidator` roda
os validadores em paralelo e propaga erros de execução. Sintaxe inválida continua sendo
um resultado bloqueante que pode provocar reparo; a conversão desse resultado usa
`asyncio.gather`, sem `try/except`.

### AD-07 · Progresso e persistência de falhas HTTP

`record_start` grava `running` e commita antes da chamada ao LLM; `record_result` grava a
conclusão normal. Antes de cada node, `_tracked` atualiza um `PipelineProgress` por requisição
com o ID, o passo atual e o resultado já produzido, sem interceptar exceções.
O handler global usa esse snapshot para gravar `failure` em uma transação curta antes
da resposta HTTP, preservando código e etapas concluídas.

A API/Studio do LangGraph registra início e conclusões normais pelo mesmo graph. Exceções
nessa entrada são tratadas pelo servidor LangGraph, não pelo handler FastAPI; sem esse
handler, a linha interrompida permanece `running`.

### AD-08 · Unit of Work + repositories específicos

`UnitOfWork` expõe `modernizations` e controla `commit/rollback`; repositories só fazem `flush`.
Sair do `async with` sem `commit()` descarta tudo (testado). Adicionar `evaluation_results` ou
`llm_calls`: novo model em `app/features/modernization/infrastructure/persistence/models/`, novo repository, novo mapper, um atributo no
`UnitOfWork` e uma migration — nenhum módulo existente precisa mudar. O port expressa necessidades
do domínio (`save`, `update`, `find_by_id`), não um CRUD genérico.

### AD-09 · `ModernizationPipeline` como port

O caso de uso depende de um Protocol, não de LangGraph. O graph (`app/features/modernization/graph`) é um adapter que
implementa esse port. Isso mantém o service testável e o framework de orquestração trocável.
O contrato do port inclui registrar a run (AD-07): qualquer implementação deve persistir
`running` antes e a conclusão normal depois; erros propagam, com progresso opcional para o handler HTTP.

### AD-10 · Pydantic no domínio

Modelos de domínio são `ValueObject` (Pydantic `frozen`, `extra="forbid"`): imutáveis, validados
e serializáveis para JSONB sem camada extra. Pydantic é uma lib de modelagem, não um vendor de
infraestrutura; o trade-off é aceitar essa dependência no núcleo em troca de muito menos código
de serialização.

### AD-11 · Composition root explícito

`app/core/bootstrap.py` é o único módulo que importa adapters concretos e monta tudo por construtor
(sem framework de DI, sem service locator). `build_container(settings)` monta engine, graph e
service uma vez; `default_container()` (`@cache`) guarda esse container **por processo**.

O `langgraph dev` serve dois pontos de entrada no mesmo processo — o lifespan da API FastAPI e a
factory `make_graph()` do `langgraph.json` — e ambos usam `default_container()`: um engine (pool de
conexões), um graph, uma leitura de `Settings`. Verificado no container: após uma execução por
`/modernize` e outra por `/runs/wait`, o app mantém **1** conexão no Postgres (eram 2 pools).
Detalhe necessário: o `langgraph.json` referencia módulos (`app.core.bootstrap:make_graph`), não
arquivos (`./app/core/bootstrap.py:...`) — por arquivo, o servidor executa o módulo de novo com outro
nome, e o cache (e o engine) duplicaria.

A entrega é que varia por ponto de entrada: `Depends` nas rotas (via `app.state`), factory no
`langgraph.json`, construtor nos services/nodes. Testes e scripts chamam `build_container` ou
injetam o próprio container por `create_app(container_factory=...)`.

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

Quando a validação reprova (AST bloqueante **ou** Ruff), a geração roda de novo com
`RepairFeedback` (código anterior + lista de problemas) no prompt. Observado nos anexos: em execuções
diferentes, C e D foram reprovados na 1ª tentativa (Ruff) e aprovados na 2ª — ver
`examples/results/SUMMARY.md`, coluna "tentativas". Limites, porque o `POST /modernize` é
síncrono e cada tentativa é outra chamada ao LLM:

- `GENERATION_MAX_ATTEMPTS=2` (uma retentativa): cobre os erros pontuais observados; pior caso
  ≈ 2× a latência de geração.
- `GENERATION_RETRY_BUDGET_SECONDS=90`: não inicia retentativa em run que já passou do orçamento
  (uma chamada lenta de 140 s ao provider não vira 280 s de espera).
- Retentativa que falha na geração (ex.: JSON inválido) não apaga a anterior: o relatório mantém o
  código da tentativa anterior no snapshot; o handler HTTP registra `failure` preservando esse código.

Trade-off: retentar também por lint (não bloqueante) custa uma chamada a mais em troca de código
limpo; `GENERATION_MAX_ATTEMPTS=1` desliga o loop. Para requests que não podem esperar, o caminho é
assíncrono (ver Evolução futura).

---

## Limitações conhecidas

- **Só `LANGUAGE plpgsql`**; funções `LANGUAGE sql` são rejeitadas com `ParsingError`.
  Arquivos com vários `CREATE FUNCTION` usam o primeiro.
- **Sem catálogo**: tipos `%TYPE`/`%ROWTYPE` não são resolvidos. Parâmetros `%TYPE` são
  reescritos com um placeholder antes da compilação, preservando o tipo original no IR e
  emitindo um warning. Erros de parsing propagam para o handler global.
- **Builtins vs routines do usuário** é heurística (lista de funções conhecidas + `pg_catalog.`);
  sem acesso ao banco não dá para ter certeza.
- **SQL dinâmico** (`EXECUTE`) não é analisável estaticamente: vira risco `DYNAMIC_SQL`.
- **Validação é estática**: `ast.parse` + Ruff não provam equivalência semântica, e o pipeline não
  executa o código gerado contra um banco. Medido nos anexos: 5/5 passam na validação estática,
  mas só 1/5 se comporta como o original quando executado (ver
  [Decisões de tradução por anexo](#decisões-de-tradução-por-anexo)). O status `success` significa
  "Python válido e sem lint", **não** "tradução correta".
- `POST /modernize` é síncrono (a request espera o LLM, incluindo a eventual retentativa — AD-13).

## Trade-offs

| escolha                                   | ganho                                   | custo                                      |
|-------------------------------------------|-----------------------------------------|--------------------------------------------|
| LLM gera, validadores determinísticos checam | tradução de casos que regras não cobrem | não determinismo; exige relatório/validação |
| OpenRouter via SDK oficial                | muitos modelos, contrato próprio do SDK | hop extra, markup; `json_object` depende do modelo |
| Duas transações por execução              | rastreabilidade mesmo com crash         | estado `running` intermediário visível     |
| Pydantic no domínio                       | serialização/validação grátis           | dependência de lib no núcleo               |
| Ruff como subprocess                      | isolamento, mesma versão do projeto     | custo de processo por validação (~ms)      |
| API síncrona                              | simples de explicar e testar            | request longa; ver evolução (fila/async)   |

---

## Evolução futura

- `AnthropicProvider`/`GeminiProvider` nativos (implementam `LLMProvider`); fallback/roteamento entre providers.
- Tabela `llm_calls` (prompt, resposta, tokens, custo) e `evaluation_results` — via `UnitOfWork`.
- **Validação dinâmica (prioridade 1):** um validador que roda o código gerado contra um Postgres
  efêmero com o schema informado e compara o estado final com a procedure original instalada no
  mesmo banco (é o procedimento usado, à mão, na seção por anexo). Os erros comportamentais entram
  no mesmo loop de reparo do AD-13.
- **Regras de prompt para as classes de erro observadas:** `CAST` explícito em bind params
  (asyncpg), `async with conn.begin_nested()` (nunca sem `await`), `NUMERIC` → `Decimal`,
  preservar o arredondamento de variáveis `NUMERIC(p,s)` intermediárias, `UPDATE … FROM` só com
  fonte agregada por chave, `EXCEPTION` do PL/pgSQL → savepoint em volta do bloco protegido.
- Resolver `%TYPE`/`%ROWTYPE` e builtins consultando o catálogo quando houver conexão disponível.
- Métricas de avaliação por modelo (taxa de sucesso, lint, custo, latência) para comparar LLMs.
- Execução assíncrona (`202 Accepted` + polling em `/modernizations/{id}`) ou via runs do servidor LangGraph.
- Novos dialetos (T-SQL, PL/SQL) implementando `SQLParser` sobre o mesmo IR.
