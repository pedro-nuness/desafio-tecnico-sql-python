"""What the behavior harness runs (a scenario of cases) and what it produces (case results)."""

from enum import StrEnum
from typing import Any

from pydantic import Field

from app.shared.domain.value_object import ValueObject


class CaseSource(StrEnum):
    USER = "user"
    """Sent by the caller (or read from the experiment's dataset): the reference."""
    GENERATED = "generated"
    """Proposed by the LLM and kept because the original routine runs it (case_generation)."""


class Case(ValueObject):
    """One call of the routine, on the original and on the generated code."""

    name: str
    sql: str
    """How the original routine is called (SELECT for functions, CALL for procedures)."""
    args: tuple[Any, ...] = ()  # YAML scalars: also dates, which are not JSON
    """Positional arguments of the generated entry point; converted to the routine's IN
    parameter types (e.g. "50.00" -> Decimal for NUMERIC, 2026-09-15 -> date)."""
    source: CaseSource = CaseSource.USER


class Scenario(ValueObject):
    """The data and the calls the caller provides to verify behavior (POST /modernize
    `behavior`). Without one, the behavior check does not run."""

    setup_sql: str
    """Tables, seed rows and any routine the original calls (installed before it)."""
    cases: tuple[Case, ...] = Field(min_length=1)
    compare_tables: tuple[str, ...] = ()
    """Tables whose final rows must match; empty = every table the setup creates."""
    ignore_columns: tuple[str, ...] = ()
    """Columns that legitimately differ between the two runs (new ids, now())."""


class CaseResult(ValueObject):
    """One case, run on the original and on the generated code."""

    name: str
    passed: bool
    detail: str
    """Why it is (not) equivalent, e.g. which table differs."""
    original: str
    """Observed outcome of the original routine (rows or error), for the report."""
    generated: str
    source: CaseSource = CaseSource.USER
    holdout: bool = False
    """Set only by the evaluation experiment (scenarios.yml): a case the pipeline was never
    given, so it never reached the LLM."""
