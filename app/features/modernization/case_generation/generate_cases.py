"""Case generation step: the LLM proposes inputs, the original routine filters them.

The LLM never writes expected results: the behavior check compares the original against the
generated code. A proposed case is kept only if it fits the signature, calls the routine with
literal values (no subquery) and runs on the original as valid SQL. The caller's cases stay
the reference; generated ones are added to them, on the caller's seed when there is one.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.features.modernization.case_generation.domain import CaseGenerationResult
from app.features.modernization.case_generation.prompt import MAX_CASES, build_cases_prompt
from app.features.modernization.parsing.domain import ParsedProcedure
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseSource,
    Scenario,
)
from app.features.modernization.validation.checks.behavior.generated import INPUT_MODES
from app.features.modernization.validation.checks.behavior.harness import BehavioralEquivalence
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.llm import LLM, LLMRequest, ResponseFormat


class _CasePayload(BaseModel):
    name: str
    sql: str
    args: list[Any] = []


class _CasesPayload(BaseModel):
    """Contract the LLM must answer with (see SYSTEM_PROMPT). Extra keys are ignored."""

    seed: str | None = None
    ignore_columns: list[str] = []
    cases: list[object] = []
    """Validated one by one: a malformed case is discarded, not the whole answer."""


class GenerateCases:
    def __init__(
        self,
        llm: LLM,
        equivalence: BehavioralEquivalence,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> None:
        self._llm = llm
        self._equivalence = equivalence
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    @property
    def available(self) -> bool:
        """Filtering runs the original routine: without the evaluation database, no step."""
        return self._equivalence.configured

    async def execute(
        self,
        *,
        procedure: ParsedProcedure,
        source_code: str,
        schema_context: str,
        behavior: Scenario | None,
    ) -> tuple[Scenario | None, CaseGenerationResult]:
        """The run's scenario (the caller's cases + the kept ones; None when there are no
        cases at all) and what happened to each proposed case."""
        prompt = build_cases_prompt(
            procedure=procedure,
            source_code=source_code,
            schema_context=schema_context,
            behavior=behavior,
        )
        response = await self._llm.generate(
            LLMRequest(
                system_prompt=prompt.system,
                user_prompt=prompt.user,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
                response_format=ResponseFormat.JSON,
            )
        )
        # Needed: an answer outside the contract is an integration failure (the node turns it
        # into a warning), not a raw JSON/pydantic error.
        try:
            payload = _parse(response.content)
        except ValueError as exc:  # JSONDecodeError and pydantic's ValidationError included
            raise IntegrationError(
                f"LLM case generation answer does not match the contract: {exc}",
                finish_reason=response.finish_reason,
            ) from exc

        warnings: list[str] = []
        if behavior is None:
            setup = f"{schema_context}\n{payload.seed or ''}"
            compare_tables: tuple[str, ...] = ()
            ignore_columns = tuple(payload.ignore_columns)
        else:
            if payload.seed:
                warnings.append("Ignored the proposed seed: the caller's setup is kept.")
            setup = behavior.setup_sql
            compare_tables, ignore_columns = behavior.compare_tables, behavior.ignore_columns

        taken = {case.name for case in behavior.cases} if behavior else set()
        candidates, discarded = _proposed_cases(payload.cases, procedure, taken)
        reasons = (
            await self._equivalence.probe(
                source_code=source_code, setup_sql=setup, cases=candidates
            )
            if candidates
            else ()
        )
        kept = [case for case, reason in zip(candidates, reasons, strict=True) if reason is None]
        discarded += [
            f"{case.name}: {reason}"
            for case, reason in zip(candidates, reasons, strict=True)
            if reason is not None
        ]

        cases = (*(behavior.cases if behavior else ()), *kept)
        scenario = (
            Scenario(
                setup_sql=setup,
                cases=cases,
                compare_tables=compare_tables,
                ignore_columns=ignore_columns,
            )
            if cases
            else None
        )
        return scenario, CaseGenerationResult(
            seed_generated=behavior is None and bool(payload.seed),
            kept=tuple(case.name for case in kept),
            discarded=tuple(discarded),
            warnings=tuple(warnings),
            provider=response.provider,
            model=response.model,
            prompt_version=prompt.version,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
        )


def _parse(content: str) -> _CasesPayload:
    """Accept a bare JSON object, optionally wrapped in prose or Markdown fences."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the answer")
    return _CasesPayload.model_validate(json.loads(content[start : end + 1]))


def _proposed_cases(
    items: list[object], procedure: ParsedProcedure, taken: set[str]
) -> tuple[list[Case], list[str]]:
    """The proposed cases that fit the routine (static checks), and why the others do not."""
    inputs = sum(1 for p in procedure.parameters if p.mode in INPUT_MODES)
    routine = procedure.name.lower()
    kept: list[Case] = []
    discarded: list[str] = []
    for index, item in enumerate(items, start=1):
        # Needed: one malformed case is discarded with its reason, the others are kept.
        try:
            proposed = _CasePayload.model_validate(item)
        except ValidationError:
            discarded.append(f"case #{index}: not in the contract (name, sql, args)")
            continue
        if len(kept) == MAX_CASES:
            discarded.append(f"{proposed.name}: over the limit of {MAX_CASES} cases")
        elif len(proposed.args) != inputs:
            discarded.append(
                f"{proposed.name}: {len(proposed.args)} args, the routine takes {inputs}"
            )
        elif routine not in proposed.sql.lower():
            discarded.append(f"{proposed.name}: does not call {routine}")
        elif _has_subquery(proposed.sql):
            discarded.append(f"{proposed.name}: the call must take literal values, not a subquery")
        else:
            name = proposed.name if proposed.name not in taken else f"{proposed.name} #{index}"
            taken.add(name)
            kept.append(
                Case(
                    name=name,
                    sql=proposed.sql,
                    args=tuple(proposed.args),
                    source=CaseSource.GENERATED,
                )
            )
    return kept, discarded


def _has_subquery(sql: str) -> bool:
    """A SELECT inside the call: the original resolves it, but the generated code only gets the
    literal `args`, so the two sides would run on different inputs (observed: args [null])."""
    selects = len(re.findall(r"\bselect\b", sql, re.IGNORECASE))
    return selects > (1 if sql.lstrip().lower().startswith("select") else 0)
