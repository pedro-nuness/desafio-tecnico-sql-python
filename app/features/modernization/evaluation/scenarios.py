"""Evaluation dataset: the legacy schema, the seed data and the cases per routine (YAML).

Paths inside the YAML are relative to it. Loaded once and validated, so a broken dataset
fails with a clear message instead of in the middle of an evaluation.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sql: str
    """How the original routine is called (SELECT for functions, CALL for procedures)."""
    args: tuple[Any, ...] = ()  # YAML scalars: also dates, which are not JSON
    """Positional arguments of the generated entry point; converted to the routine's IN
    parameter types (e.g. "50.00" -> Decimal for NUMERIC, 2026-09-15 -> date)."""
    holdout: bool = False
    """Never shown to the LLM: only the metric runs it (the repair loop runs the others)."""


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requires: tuple[Path, ...] = ()
    """Other routines the original calls (installed before it), relative to the YAML."""
    cases: tuple[Case, ...] = Field(min_length=1)


class _DatasetFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Path = Field(alias="schema")
    seed: Path
    compare_tables: tuple[str, ...] = Field(min_length=1)
    ignore_columns: tuple[str, ...] = ()
    scenarios: dict[str, Scenario]


class Dataset:
    """Setup SQL, compared tables and the scenario of each routine (by lowercase name)."""

    def __init__(
        self,
        *,
        setup_sql: str,
        compare_tables: tuple[str, ...],
        ignore_columns: tuple[str, ...],
        scenarios: dict[str, Scenario],
        base_dir: Path,
    ) -> None:
        self.setup_sql = setup_sql
        """Annex A schema followed by the seed."""
        self.compare_tables = compare_tables
        self.ignore_columns = ignore_columns
        self._scenarios = {name.lower(): scenario for name, scenario in scenarios.items()}
        self._base_dir = base_dir

    @classmethod
    def load(cls, path: Path) -> Dataset:
        parsed = _DatasetFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        base_dir = path.parent
        return cls(
            setup_sql=_read(base_dir / parsed.schema_) + "\n" + _read(base_dir / parsed.seed),
            compare_tables=parsed.compare_tables,
            ignore_columns=parsed.ignore_columns,
            scenarios=parsed.scenarios,
            base_dir=base_dir,
        )

    def scenario(self, routine: str) -> Scenario | None:
        return self._scenarios.get(routine.lower())

    def routines(self) -> tuple[str, ...]:
        return tuple(self._scenarios)

    def requirements_sql(self, scenario: Scenario) -> str:
        return "\n".join(_read(self._base_dir / path) for path in scenario.requires)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
