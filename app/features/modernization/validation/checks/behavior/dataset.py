"""Evaluation dataset of the experiment: the legacy schema, the seed data and the cases per
routine (YAML).

Only the experiment reads it (scripts/run_examples.py and the evaluation endpoint): the
pipeline runs the scenario its caller sends. The YAML splits the cases into dev (sent to the
pipeline) and holdout (only the metric runs them), so the metric measures generalization.

Paths inside the YAML are relative to it. Loaded once and validated, so a broken dataset
fails with a clear message instead of in the middle of an evaluation.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.features.modernization.validation.checks.behavior.domain import Case, Scenario


class _Case(Case):
    holdout: bool = False
    """Never sent to the pipeline, so never shown to the LLM: only the metric runs it."""


class _Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requires: tuple[Path, ...] = ()
    """Other routines the original calls (installed before it), relative to the YAML."""
    cases: tuple[_Case, ...] = Field(min_length=1)


class _DatasetFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Path = Field(alias="schema")
    seed: Path
    compare_tables: tuple[str, ...] = Field(min_length=1)
    ignore_columns: tuple[str, ...] = ()
    scenarios: dict[str, _Scenario]


class Dataset:
    """Setup SQL, compared tables and the scenario of each routine (by lowercase name)."""

    def __init__(
        self,
        *,
        setup_sql: str,
        compare_tables: tuple[str, ...],
        ignore_columns: tuple[str, ...],
        scenarios: dict[str, _Scenario],
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

    def scenario(self, routine: str, *, include_holdout: bool) -> Scenario | None:
        """The routine's cases as the harness runs them: without the holdout cases for the
        pipeline, with them for the metric. None when the dataset has no scenario for it."""
        found = self._scenarios.get(routine.lower())
        if found is None:
            return None
        requirements = (_read(self._base_dir / path) for path in found.requires)
        return Scenario(
            setup_sql="\n".join((self.setup_sql, *requirements)),
            cases=tuple(
                Case(name=case.name, sql=case.sql, args=case.args)
                for case in found.cases
                if include_holdout or not case.holdout
            ),
            compare_tables=self.compare_tables,
            ignore_columns=self.ignore_columns,
        )

    def holdout(self, routine: str) -> frozenset[str]:
        """Names of the routine's holdout cases."""
        found = self._scenarios.get(routine.lower())
        cases = found.cases if found else ()
        return frozenset(case.name for case in cases if case.holdout)

    def routines(self) -> tuple[str, ...]:
        return tuple(self._scenarios)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
