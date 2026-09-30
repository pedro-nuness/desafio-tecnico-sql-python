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
| `app`      | FastAPI/uvicorn em `:8000` (só inicia após `migrate` terminar com 0)   |

Sem configuração nenhuma o compose usa `LLM_PROVIDER=fake` (saída determinística, sem chave),
suficiente para exercitar o vertical slice inteiro. Para um LLM real:

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
uv run uvicorn app.api.main:app --reload
```

### Servidor LangGraph (`langgraph dev` + Studio)

```bash
uv run langgraph dev --allow-blocking
```

O `langgraph.json` registra o graph `modernization` (factory `app/bootstrap.py:make_graph`) e monta
a própria API FastAPI via `http.app`, então o mesmo servidor expõe:

- API do LangGraph (`/assistants`, `/threads`, `/runs/...`) e o Studio;
- `GET /health`, `POST /modernize`, `GET /modernizations/{id}` (nossas rotas, com lifespan combinado).

> `--allow-blocking`: o `langgraph dev` usa *blockbuster* para acusar I/O síncrono no event loop.
> O `asyncpg` resolve `~/.postgresql` (defaults de SSL) ao abrir cada conexão do pool, o que é
> acusado como bloqueante. É interno do driver, custa microssegundos e não existe no uvicorn/Docker.

### Anexos B–F (casos de teste do desafio)

`examples/schema.sql` (Anexo A) e `examples/procedures/*.sql` (Anexos B–F) são o material do
desafio. O script roda os cinco pelo mesmo caso de uso do `POST /modernize` (portanto cada
execução também é persistida em `modernization_history`), com o schema como contexto:

```bash
uv run python -m scripts.run_examples
```

Saída em `examples/results/`: `<procedure>/generated.py`, `<procedure>/report.json` e
[`SUMMARY.md`](examples/results/SUMMARY.md) (status, estratégia, riscos, Ruff, tokens, latência).
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
    LangGraph --> Parsing
    Parsing --> Analysis
    Analysis --> Generation
    Generation --> Validation

    Parsing --> SQLParser
    Analysis --> SemanticAnalyzer
    Generation --> CodeGenerationService
    CodeGenerationService --> GenerationPromptBuilder
    CodeGenerationService --> LLMProvider
    Validation --> CodeValidator

    ModernizationService --> UnitOfWork
    UnitOfWork --> Repository
    Repository --> PostgreSQL
```

### Ports & Adapters

```mermaid
flowchart TB
    subgraph Driving["Driving adapters"]
        FASTAPI["FastAPI routes<br/>app/api"]
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

    subgraph Driven["Driven adapters (app/infrastructure, app/graph)"]
        A1["PglastParser"]
        A2["OpenAIProvider (openai / openrouter)<br/>FakeLLMProvider"]
        A3["PythonASTValidator · RuffValidator<br/>CompositeCodeValidator"]
        A4["SqlAlchemyUnitOfWork<br/>SqlAlchemyModernizationRepository"]
        A5["LangGraphModernizationPipeline"]
    end

    FASTAPI --> SVC
    LGCLI --> A5
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
api ──► application ──► ports ◄── infrastructure
             │                          │
             └──────► domain ◄──────────┘
graph (LangGraph) ──► application/ports + domain
bootstrap.py = composition root (único lugar que conhece todos os adapters)
```

### Eixos de variação (onde existem abstrações — e só lá)

| Eixo                 | Port                                   | Adapters hoje                                         |
|----------------------|----------------------------------------|-------------------------------------------------------|
| LLM provider/modelo  | `LLMProvider`                          | `OpenAIProvider` (OpenAI e OpenRouter), `FakeLLMProvider` |
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
    START((START)) --> parsing
    parsing -- ok --> semantic_analysis
    semantic_analysis -- ok --> generation
    generation -- ok --> validation
    validation --> END((END))
    parsing -. falhou .-> END
    semantic_analysis -. falhou .-> END
    generation -. falhou .-> END
```

- **Estado tipado** (`app/graph/state.py`): `ModernizationState` (`TypedDict`) com modelos de domínio
  explícitos (`ParsedProcedure`, `SemanticAnalysis`, `GenerationResult`, `ValidationResult`) e canais
  append-only (`completed_steps`, `warnings`, `errors`) via reducer `operator.add`.
- **Nodes finos** (`app/graph/nodes/`): cada um chama um port/serviço, converte erros *esperados*
  do domínio em `PipelineError` e devolve um `StateUpdate` parcial. Nenhum node monta prompt,
  conhece vendor ou chama `ast`/Ruff diretamente.
- **Short-circuit**: se um passo falha, arestas condicionais levam a `END` (o graph desenhado
  continua sendo `START → parsing → semantic_analysis → generation → validation → END`).
- **Dependências injetadas nos nodes** (instâncias callable construídas no builder), não via
  `config`/`context` — assim o mesmo graph roda no uvicorn e no servidor LangGraph.
- **Resiliência**: `LangGraphModernizationPipeline` consome `astream(stream_mode="values")` e guarda
  o último snapshot. Se algo inesperado escapar de um node, o outcome ainda contém os passos já
  concluídos e o erro é atribuído ao passo seguinte. O port garante: `run()` nunca lança.

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

Falhas do pipeline **não** viram HTTP 5xx: a execução foi registrada e o corpo diz o que aconteceu
(`status: failure|partial` + `report.errors`). Entrada inválida (ex.: `source_code` vazio) → 422.

---

## Relatório

`ModernizationReport` (domínio, Pydantic, serializado com `model_dump(mode="json")` para JSONB):

| seção               | conteúdo                                                                                          |
|---------------------|---------------------------------------------------------------------------------------------------|
| `parsing`           | `success`, `procedure_name`, `kind`, `parser`, parâmetros/modos, `return_type`, tabelas, funções, `warnings` |
| `semantic_analysis` | `detected_features` (com ocorrências e linhas), `risks` (`code`, `severity`, `message`, `line`), `dependencies`, `recommended_strategy`, `recommendations` |
| `generation`        | `strategy` (escolhida pelo LLM), `recommended_strategy` (determinística), `provider`, `model`, `prompt_version`, tokens, `latency_ms`, `finish_reason`, `architectural_decisions`, `warnings` |
| `validation`        | `valid_python`, `passed_all`, resultado por validador (`validator`, `success`, `blocking`, `messages`), `warnings` |
| raiz                | `completed_steps`, `errors` (`step`, `error_type`, `message`), `warnings`                          |

### Status

| status    | quando                                                                              |
|-----------|-------------------------------------------------------------------------------------|
| `running` | gravado **antes** do pipeline (se o processo morrer, a execução fica visível)        |
| `success` | código gerado e todos os validadores passaram                                        |
| `partial` | código gerado e sintaticamente válido, mas Ruff reportou algo, ou a validação não rodou |
| `failure` | nenhum código utilizável: falha em parsing/análise/geração ou Python inválido (`ast.parse`) |

A regra fica em `PipelineOutcome.status()` (domínio), testada isoladamente.

---

## Configuração

Centralizada em `app/config/settings.py` (`pydantic-settings`; lê env vars e `.env`). Nenhum
`os.getenv` espalhado. Copie `.env.example` para `.env` (git-ignored).

| variável                | default                                                       | descrição                                  |
|-------------------------|---------------------------------------------------------------|--------------------------------------------|
| `DATABASE_URL`          | `postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer` | driver async obrigatório          |
| `LLM_PROVIDER`          | `fake`                                                        | `fake` \| `openai` \| `openrouter`         |
| `LLM_MODEL`             | `fake-model`                                                  | ex.: `anthropic/claude-sonnet-4.5` (OpenRouter), `gpt-...` (OpenAI) |
| `LLM_API_KEY`           | —                                                             | obrigatório para `openai`/`openrouter` (falha no startup se ausente) |
| `LLM_BASE_URL`          | —                                                             | qualquer endpoint OpenAI-compatible        |
| `LLM_TEMPERATURE`       | `0.0`                                                         |                                            |
| `LLM_MAX_OUTPUT_TOKENS` | `8192`                                                        |                                            |
| `LLM_TIMEOUT_SECONDS`   | `120`                                                         |                                            |
| `LLM_MAX_RETRIES`       | `2`                                                           | retries do SDK                             |
| `LLM_REASONING_EFFORT`  | — (não enviado)                                               | `minimal`\|`low`\|`medium`\|`high`, só para modelos de raciocínio (ver AD-04) |
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

- **Unitários (59)**: parser (`pglast`), análise semântica (inclusive sobre IR montado à mão, sem
  parser), `CodeGenerationService` com `FakeLLMProvider` (verifica que o prompt carrega parsing +
  análise), validadores (AST, Ruff, composite), `ModernizationService` com LangGraph real + UoW em
  memória (sucesso, falha do LLM, falha de parsing, Python inválido, lint → partial, crash
  inesperado), API (`/health`, `/modernize`, `/modernizations/{id}`), factory de providers e
  **regras de dependência entre camadas** (análise de imports via `ast`).
- **Integração (5)**: Postgres real migrado com Alembic (subprocess), round-trip do repository,
  JSONB, rollback sem commit, e persistência de sucesso **e** falha ponta a ponta.
  São pulados quando `TEST_DATABASE_URL` não está definido. O `docker compose` cria o banco
  `modernizer_test` (`docker/postgres/init`).
- Nenhum teste chama OpenAI/OpenRouter.

---

## Estrutura de pastas

```
app/
├── api/                      # FastAPI (driving adapter)
│   ├── main.py               # create_app + lifespan; também montado pelo LangGraph (http.app)
│   ├── dependencies.py
│   ├── routes/               # health.py, modernization.py
│   └── schemas/              # modernization_request.py, modernization_response.py
├── application/
│   ├── ports/                # Protocols: llm/, parsing/, validation/, repositories/, pipeline/
│   └── services/             # modernization_service.py, code_generation_service.py
├── domain/
│   ├── enums/                # ModernizationStatus, GenerationStrategy, PipelineStep
│   ├── exceptions/
│   ├── models/               # parsing (IR), semantic_analysis, generation, validation, modernization (+report)
│   └── services/             # semantic_analyzer.py (puro, determinístico)
├── graph/
│   ├── state.py · builder.py · pipeline.py
│   └── nodes/                # parsing, semantic_analysis, generation, validation
├── infrastructure/
│   ├── llm/                  # openai_provider.py, fake_provider.py, provider_factory.py
│   ├── parsing/              # pglast_parser.py (único import de pglast)
│   ├── validation/           # python_ast_validator.py, ruff_validator.py, composite_validator.py
│   └── persistence/
│       ├── database/         # base, engine, session, unit_of_work
│       ├── models/           # uma classe ORM por arquivo
│       ├── repositories/     # um repository por aggregate
│       └── mappers/          # ORM <-> domínio
├── prompts/generation_prompt.py
├── config/settings.py
└── bootstrap.py              # composition root + make_graph() para o langgraph.json
migrations/                   # Alembic (env async + versions/)
examples/                     # Anexo A (schema.sql), Anexos B–F (procedures/) e results/
scripts/run_examples.py       # roda os anexos pelo ModernizationService e grava results/
tests/{unit,integration,fixtures/procedures}
docker/postgres/init/         # cria o banco de testes
```

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

### AD-04 · LLM atrás de `LLMProvider`; OpenRouter via adapter OpenAI-compatible

`LLMRequest`/`LLMResponse` são modelos próprios (provider, model, tokens, latência,
`finish_reason`). `OpenAIProvider` fala Chat Completions e aceita `base_url`, então serve OpenAI,
**OpenRouter** (uma chave → Claude, Gemini, GPT, Llama só trocando `LLM_MODEL`) e endpoints
self-hosted. Erros do SDK viram `LLMProviderError`. A seleção acontece em
`infrastructure/llm/provider_factory.py` (um `match`), chamada só pelo composition root.
Adicionar `AnthropicProvider`/`GeminiProvider` = um arquivo novo + um `case`; nodes e services
não mudam.

Modelos de raciocínio contam os tokens de "pensamento" dentro de `max_completion_tokens`. Medido
com `z-ai/glm-5.3-flash` no Anexo F: sem limite, gastou >32k tokens pensando e devolveu conteúdo
vazio (`finish_reason=length`, ~180 s); com `LLM_REASONING_EFFORT=low`, ~2k tokens e ~11 s, com a
resposta completa. Por isso o esforço é configurável (enviado como `reasoning_effort`, que
OpenAI e OpenRouter aceitam) e, quando a resposta é cortada por limite, o erro diz isso
explicitamente em vez de "JSON inválido". Se o modelo omitir `strategy` no contrato, usa-se a
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
os validadores em paralelo e transforma "validador não conseguiu rodar" em resultado não bloqueante,
sem esconder os demais.

### AD-07 · Toda execução é persistida (duas transações curtas)

`ModernizationService`: (1) grava `running` e **commita antes** da chamada ao LLM; (2) roda o
pipeline (que nunca lança); (3) grava o estado final. Transação longa segurando conexão durante uma
chamada de LLM de dezenas de segundos seria pior, e um crash no meio deixaria nada gravado. Optei
por `running` em vez de "registrar como `partial`" para não misturar "em andamento" com "concluído
com ressalvas"; um `running` antigo é detectável como execução abortada.

### AD-08 · Unit of Work + repositories específicos

`UnitOfWork` expõe `modernizations` e controla `commit/rollback`; repositories só fazem `flush`.
Sair do `async with` sem `commit()` descarta tudo (testado). Adicionar `evaluation_results` ou
`llm_calls`: novo model em `persistence/models/`, novo repository, novo mapper, um atributo no
`UnitOfWork` e uma migration — nenhum módulo existente precisa mudar. O port expressa necessidades
do domínio (`save`, `update`, `find_by_id`), não um CRUD genérico.

### AD-09 · `ModernizationPipeline` como port

O caso de uso depende de um Protocol, não de LangGraph. O graph (`app/graph`) é um adapter que
implementa esse port. Isso mantém o service testável e o framework de orquestração trocável.

### AD-10 · Pydantic no domínio

Modelos de domínio são `ValueObject` (Pydantic `frozen`, `extra="forbid"`): imutáveis, validados
e serializáveis para JSONB sem camada extra. Pydantic é uma lib de modelagem, não um vendor de
infraestrutura; o trade-off é aceitar essa dependência no núcleo em troca de muito menos código
de serialização.

### AD-11 · Composition root explícito

`app/bootstrap.py` monta tudo por construtor (sem container de DI, sem singletons globais, sem
service locator). A API guarda o `Container` em `app.state` no lifespan e o injeta via `Depends`.
Testes substituem o container inteiro por `create_app(container_factory=...)`.

### AD-12 · Docker

Imagem `python:3.14-slim` + `uv sync --frozen --no-dev` (lockfile), usuário não-root. Migrations
rodam num serviço one-shot (`migrate`), e o `app` depende de
`service_completed_successfully` — a app nunca sobe com schema desatualizado, e múltiplas réplicas
não competem pela migration.

---

## Limitações conhecidas

- **Só `LANGUAGE plpgsql`**; funções `LANGUAGE sql` são rejeitadas com `ParsingError`.
  Arquivos com vários `CREATE FUNCTION` usam o primeiro.
- **Sem catálogo**: tipos `%TYPE`/`%ROWTYPE` não são resolvidos. `%TYPE` em **parâmetro** faz o
  compilador do libpg_query responder "Not implemented"; o adapter reescreve o AST do
  `CreateFunctionStmt` com um tipo placeholder, faz deparse e reparse, mantém o tipo original no IR
  e emite warning.
- **Builtins vs routines do usuário** é heurística (lista de funções conhecidas + `pg_catalog.`);
  sem acesso ao banco não dá para ter certeza.
- **SQL dinâmico** (`EXECUTE`) não é analisável estaticamente: vira risco `DYNAMIC_SQL`.
- **Validação é estática**: `ast.parse` + Ruff não provam equivalência semântica. O código gerado
  não é executado contra um banco.
- `POST /modernize` é síncrono (a request espera o LLM).
- O `FakeLLMProvider` devolve um módulo placeholder — serve para exercitar o pipeline, não traduz.

## Trade-offs

| escolha                                   | ganho                                   | custo                                      |
|-------------------------------------------|-----------------------------------------|--------------------------------------------|
| LLM gera, validadores determinísticos checam | tradução de casos que regras não cobrem | não determinismo; exige relatório/validação |
| OpenRouter via adapter OpenAI             | um adapter, muitos modelos              | hop extra, markup; `json_object` depende do modelo |
| Duas transações por execução              | rastreabilidade mesmo com crash         | estado `running` intermediário visível     |
| Pydantic no domínio                       | serialização/validação grátis           | dependência de lib no núcleo               |
| Ruff como subprocess                      | isolamento, mesma versão do projeto     | custo de processo por validação (~ms)      |
| API síncrona                              | simples de explicar e testar            | request longa; ver evolução (fila/async)   |

---

## Evolução futura

- `AnthropicProvider`/`GeminiProvider` nativos (implementam `LLMProvider`); fallback/roteamento entre providers.
- Tabela `llm_calls` (prompt, resposta, tokens, custo) e `evaluation_results` — via `UnitOfWork`.
- Loop de auto-correção no graph: `validation → generation` com os erros do Ruff/AST (limite de tentativas).
- Validação dinâmica: executar o código gerado contra um Postgres efêmero com o schema informado e
  comparar resultados com a procedure original (testes de equivalência).
- Resolver `%TYPE`/`%ROWTYPE` e builtins consultando o catálogo quando houver conexão disponível.
- Métricas de avaliação por modelo (taxa de sucesso, lint, custo, latência) para comparar LLMs.
- Execução assíncrona (`202 Accepted` + polling em `/modernizations/{id}`) ou via runs do servidor LangGraph.
- Novos dialetos (T-SQL, PL/SQL) implementando `SQLParser` sobre o mesmo IR.
