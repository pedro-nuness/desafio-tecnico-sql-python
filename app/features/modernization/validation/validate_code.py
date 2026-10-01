"""Validation of generated code: checks are strategies, the rules decide what blocks.

A check only reports what it found; whether a finding makes the code unusable (FAILURE) or
just imperfect (PARTIAL) is policy, declared once per rule where the rules are assembled
(core/providers.py). Adding a check (mypy, bandit...) = one module here + one Rule.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.features.modernization.domain.parsing import ParsedProcedure
from app.features.modernization.domain.validation import (
    ValidationMessage,
    ValidationResult,
    ValidatorResult,
)


@dataclass(frozen=True, slots=True)
class Routine:
    """The original routine the code was generated from (behavioral checks run it too)."""

    source_code: str
    procedure: ParsedProcedure


@dataclass(frozen=True, slots=True)
class Skipped:
    """The check could not run on this code (e.g. no evaluation scenario for the routine)."""

    reason: str


type Findings = tuple[ValidationMessage, ...] | Skipped


class CodeCheck(Protocol):
    """One validation strategy. Async because some checks spawn processes.

    Returns its findings (empty = passed) or Skipped. The tool itself failing raises
    (AppError). Static checks ignore `routine`.
    """

    name: str

    async def check(self, code: str, routine: Routine | None = None) -> Findings: ...


@dataclass(frozen=True, slots=True)
class Rule:
    check: CodeCheck
    blocking: bool
    """A failing blocking check means the code is unusable (e.g. a syntax error)."""


class ValidateCode:
    """Runs every rule's check concurrently and merges the results; exceptions propagate."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        if not rules:
            raise ValueError("ValidateCode needs at least one rule")
        self._rules = tuple(rules)

    async def execute(self, code: str, routine: Routine | None = None) -> ValidationResult:
        findings = await asyncio.gather(*(rule.check.check(code, routine) for rule in self._rules))
        return ValidationResult(
            results=tuple(
                _result(rule, found) for rule, found in zip(self._rules, findings, strict=True)
            )
        )


def _result(rule: Rule, found: Findings) -> ValidatorResult:
    if isinstance(found, Skipped):
        return ValidatorResult(
            validator=rule.check.name, success=True, blocking=rule.blocking, skipped=found.reason
        )
    return ValidatorResult(
        validator=rule.check.name, success=not found, blocking=rule.blocking, messages=found
    )
