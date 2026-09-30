"""Builds the generation prompt from the deterministic analysis (not just the raw source)."""

from app.domain.enums import GenerationStrategy
from app.domain.models.parsing import ParsedProcedure, Statement
from app.domain.models.semantic_analysis import SemanticAnalysis
from app.domain.models.value_object import ValueObject

PROMPT_VERSION = "generation-v1"

SYSTEM_PROMPT = f"""\
You are a senior backend engineer modernizing PostgreSQL PL/pgSQL routines into Python 3.14.

Guiding principle: keep relational logic close to the database unless there is a clear
reason to move it. Joins, aggregates, filters, CTEs (recursive or not), bulk DML and row
locking stay as parameterized SQL executed by the Python code. Python coordinates input
validation, control flow, error handling, composition and database calls.

Translation rules:
1. Target Python 3.14 with full type hints. Produce one self-contained module.
2. Database access uses SQLAlchemy 2.x async: the public entry point is
   `async def <snake_case_name>(conn: AsyncConnection, ...)` and SQL runs through
   `sqlalchemy.text()` with named bind parameters. Never build SQL with f-strings,
   `%` or `.format()`; dynamic identifiers must come from an explicit whitelist.
3. The caller owns the transaction: do not call commit/rollback unless the original
   routine controls transactions, and then explain it as an architectural decision.
4. Row locks (FOR UPDATE / FOR SHARE) and the writes that depend on them must run on the
   same connection/transaction.
5. RAISE EXCEPTION -> a typed exception class defined in the module;
   RAISE NOTICE/INFO/WARNING -> the `logging` module; GET DIAGNOSTICS ROW_COUNT ->
   `result.rowcount`.
6. OUT/INOUT parameters -> return a frozen dataclass; RETURN QUERY / SETOF -> return a
   list of typed rows (frozen dataclasses).
7. Row-by-row loops that issue SQL per iteration (N+1) should become one set-based
   statement when semantics allow; otherwise keep them and add a warning.
8. Preserve behaviour. When something cannot be translated faithfully, keep it in SQL and
   report it in "warnings".
9. Code must pass `ast.parse` and Ruff (pyflakes/pycodestyle/bugbear), with no unused imports.

Answer with ONE JSON object and nothing else, using exactly this shape:
{{
  "python_code": "<complete Python module>",
  "strategy": "<one of: {", ".join(s.value for s in GenerationStrategy)}>",
  "architectural_decisions": [
    {{"topic": "<short>", "decision": "<what you did>", "rationale": "<why>"}}
  ],
  "warnings": ["<behaviour differences, assumptions or manual follow-ups>"]
}}
"""


class GenerationPrompt(ValueObject):
    system: str
    user: str
    version: str


class GenerationPromptBuilder:
    def build(
        self,
        *,
        procedure: ParsedProcedure,
        analysis: SemanticAnalysis,
        source_code: str,
        schema_context: str | None,
    ) -> GenerationPrompt:
        sections = [
            _signature_section(procedure),
            _declarations_section(procedure),
            _control_flow_section(procedure),
            _sql_section(procedure),
            _analysis_section(analysis),
            "## Database schema\n"
            + (schema_context.strip() if schema_context else "Not provided."),
            "## Original source (reference only; the analysis above is authoritative)\n"
            f"```sql\n{source_code.strip()}\n```",
        ]
        return GenerationPrompt(
            system=SYSTEM_PROMPT,
            user="\n\n".join(sections),
            version=PROMPT_VERSION,
        )


def _signature_section(procedure: ParsedProcedure) -> str:
    lines = [
        "## Routine",
        f"- name: {procedure.qualified_name}",
        f"- kind: {procedure.kind.value}",
        f"- returns: {_returns(procedure)}",
        "- parameters:",
    ]
    lines += [
        f"  - {p.name}: {p.data_type} [{p.mode.value}{', has default' if p.has_default else ''}]"
        for p in procedure.parameters
    ] or ["  - (none)"]
    return "\n".join(lines)


def _returns(procedure: ParsedProcedure) -> str:
    if procedure.return_type is None:
        return "nothing (procedure)"
    return f"SETOF {procedure.return_type}" if procedure.returns_set else procedure.return_type


def _declarations_section(procedure: ParsedProcedure) -> str:
    lines = ["## Declarations"]
    for declaration in procedure.declarations:
        detail = declaration.data_type or ""
        if declaration.cursor_sql:
            detail = f"cursor for: {declaration.cursor_sql.text}"
        lines.append(f"- {declaration.name} ({declaration.kind.value}) {detail}".rstrip())
    return "\n".join(lines) if len(lines) > 1 else "## Declarations\n- (none)"


def _control_flow_section(procedure: ParsedProcedure) -> str:
    lines = ["## Control-flow outline (from the PL/pgSQL AST)"]
    _outline(procedure.body, depth=0, lines=lines)
    return "\n".join(lines)


def _outline(statements: tuple[Statement, ...], depth: int, lines: list[str]) -> None:
    for statement in statements:
        lines.append(f"{'  ' * depth}- {_describe(statement)}")
        _outline(statement.body, depth + 1, lines)
        if statement.else_body:
            lines.append(f"{'  ' * depth}  else:")
            _outline(statement.else_body, depth + 2, lines)
        for handler in statement.exception_handlers:
            lines.append(f"{'  ' * depth}  exception when {' or '.join(handler.conditions)}:")
            _outline(handler.body, depth + 2, lines)


def _describe(statement: Statement) -> str:
    line = f"L{statement.line} " if statement.line else ""
    parts = [f"{line}{statement.kind.value.upper()}"]
    if statement.target:
        parts.append(f"target={statement.target}")
    if statement.expression:
        parts.append(f"expr=`{statement.expression}`")
    if statement.sql:
        parts.append(f"sql=`{statement.sql.text}`")
    if statement.into:
        parts.append("(INTO)")
    if statement.raise_level:
        message = statement.raise_message or statement.raise_condition or "re-raise"
        parts.append(f"level={statement.raise_level} message={message!r}")
    if statement.diagnostics:
        parts.append(f"items={', '.join(statement.diagnostics)}")
    return " ".join(parts)


def _sql_section(procedure: ParsedProcedure) -> str:
    lines = ["## Embedded SQL (parsed with the PostgreSQL grammar)"]
    for fragment in procedure.sql_fragments():
        facts = [f"command={fragment.command.value}"]
        if fragment.tables:
            facts.append(f"tables={', '.join(fragment.tables)}")
        if fragment.functions:
            facts.append(f"functions={', '.join(fragment.functions)}")
        if fragment.cte_names:
            recursive = " (recursive)" if fragment.has_recursive_cte else ""
            facts.append(f"cte={', '.join(fragment.cte_names)}{recursive}")
        if fragment.locking_clauses:
            facts.append(f"locking={', '.join(fragment.locking_clauses)}")
        if fragment.uses_jsonb:
            facts.append("jsonb")
        if fragment.parse_error:
            facts.append(f"UNPARSED: {fragment.parse_error}")
        lines.append(f"- `{fragment.text}` -> {'; '.join(facts)}")
    return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n- (none)"


def _analysis_section(analysis: SemanticAnalysis) -> str:
    lines = [
        "## Deterministic semantic analysis",
        f"- recommended strategy: {analysis.recommended_strategy.value} "
        "(you may choose another one if you justify it in architectural_decisions)",
        "- features: " + (", ".join(f.feature.value for f in analysis.features) or "(none)"),
        "- risks:",
    ]
    lines += [
        f"  - [{risk.severity.value}] {risk.code}"
        + (f" (L{risk.line})" if risk.line else "")
        + f": {risk.message}"
        for risk in analysis.risks
    ] or ["  - (none)"]
    lines.append("- recommendations:")
    lines += [f"  - {r.code}: {r.message}" for r in analysis.recommendations] or ["  - (none)"]
    return "\n".join(lines)
