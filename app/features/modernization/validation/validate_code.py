"""Validation of generated code: checks are strategies, the rules decide what blocks.

A check only reports what it found; whether a finding makes the code unusable (FAILURE) or
just imperfect (PARTIAL) is policy, declared once per rule where the rules are assembled
(core/providers.py). Adding a check (mypy, bandit...) = one module here + one Rule.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.features.modernization.domain.validation import (
    ValidationMessage,
    ValidationResult,
    ValidatorResult,
)


class CodeCheck(Protocol):
    """One validation strategy. Async because some checks spawn processes.

    Returns its findings (empty = passed). The tool itself failing raises (AppError).
    """

    name: str

    async def check(self, code: str) -> tuple[ValidationMessage, ...]: ...


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

    async def execute(self, code: str) -> ValidationResult:
        findings = await asyncio.gather(*(rule.check.check(code) for rule in self._rules))
        return ValidationResult(
            results=tuple(
                ValidatorResult(
                    validator=rule.check.name,
                    success=not messages,
                    blocking=rule.blocking,
                    messages=messages,
                )
                for rule, messages in zip(self._rules, findings, strict=True)
            )
        )
