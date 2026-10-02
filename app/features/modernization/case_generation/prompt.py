"""Builds the case generation prompt: the routine's signature, the schema, the source and
what the caller already sent (its setup and cases)."""

from app.features.modernization.parsing.domain import ParsedProcedure
from app.features.modernization.validation.checks.behavior.domain import Scenario
from app.shared.domain.value_object import ValueObject

PROMPT_VERSION = "cases-v1"
MAX_CASES = 6

SYSTEM_PROMPT = f"""\
You write test inputs for a PostgreSQL PL/pgSQL routine that is being rewritten in Python.
Each case runs on the ORIGINAL routine and on the rewrite, and their results and final table
rows are compared. Never write expected results: the original routine is the oracle.

Write the cases that tell a faithful rewrite from a wrong one:
1. Every path: each IF/ELSIF/ELSE branch, each RAISE, each EXCEPTION handler, loops over
   zero, one and several rows.
2. Edge values: NULL for each input, zero, negative amounts, ids that do not exist, and the
   constants in the routine's conditions (just below, at and just above them).
3. Data that breaks naive rewrites: several rows for the same key, inactive or closed rows
   next to active ones, NUMERIC values that need rounding (e.g. 0.005).
4. At most {MAX_CASES} cases, all different; the name says what the case covers.

Each case:
- "sql": how the original is called, with literal values only: `SELECT <routine>(...)` for a
  scalar function, `SELECT * FROM <routine>(...)` for SETOF/TABLE results or OUT parameters,
  `CALL <routine>(...)` for a procedure (NULL in the position of each OUT parameter).
- "args": the same IN/INOUT values, in parameter order, as JSON: numbers for integers,
  strings for NUMERIC ("50.00") and dates ("2026-09-15"), null for NULL.

"seed": SQL that fills the schema's tables for your cases: INSERT statements with explicit
ids, then `SELECT setval(pg_get_serial_sequence('<table>', 'id'), <max id>);` for every
table with a serial id you filled. When an existing setup is given, answer "seed": null and
build the cases on its rows.
"ignore_columns": columns whose values differ between two runs of the same call (ids from
sequences, now()/CURRENT_TIMESTAMP defaults).

Answer with ONE JSON object and nothing else, using exactly this shape:
{{"seed": "<SQL or null>", "ignore_columns": ["<column>"],
  "cases": [{{"name": "<what it covers>", "sql": "<SQL>", "args": [<values>]}}]}}
"""  # noqa: S608 (prompt text that mentions SQL, not a query)


class CasesPrompt(ValueObject):
    system: str
    user: str
    version: str


def build_cases_prompt(
    *,
    procedure: ParsedProcedure,
    source_code: str,
    schema_context: str,
    behavior: Scenario | None,
) -> CasesPrompt:
    sections = [_routine_section(procedure), f"## Database schema\n{schema_context.strip()}"]
    if behavior is not None:
        existing = "\n".join(f"- {case.name}: {case.sql}" for case in behavior.cases)
        sections += [
            '## Existing setup (schema and seed): build on its rows, answer "seed": null\n'
            f"```sql\n{behavior.setup_sql.strip()}\n```",
            f"## Existing cases (do not repeat them)\n{existing}",
        ]
    sections.append(f"## Original source\n```sql\n{source_code.strip()}\n```")
    return CasesPrompt(system=SYSTEM_PROMPT, user="\n\n".join(sections), version=PROMPT_VERSION)


def _routine_section(procedure: ParsedProcedure) -> str:
    lines = [
        "## Routine",
        f"- name: {procedure.qualified_name}",
        f"- kind: {procedure.kind.value}",
        f"- returns: {procedure.return_type or 'nothing (procedure)'}"
        + (" (set of rows)" if procedure.returns_set else ""),
        "- parameters, in order:",
    ]
    lines += [f"  - {p.name}: {p.data_type} [{p.mode.value}]" for p in procedure.parameters] or [
        "  - (none)"
    ]
    return "\n".join(lines)
