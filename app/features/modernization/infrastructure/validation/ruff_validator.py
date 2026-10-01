"""Runs Ruff as an isolated subprocess on the generated code (stdin, no files, no config)."""

import asyncio
import subprocess
from collections.abc import Sequence

from pydantic import BaseModel, TypeAdapter
from ruff.__main__ import find_ruff_bin

from app.features.modernization.domain.models.validation import (
    ValidationMessage,
    ValidationResult,
    ValidatorResult,
)
from app.shared.errors import AppError

DEFAULT_RULES = ("E4", "E7", "E9", "F", "B", "ASYNC", "S608")
"""Correctness-oriented rules; style (line length, import order) is not a blocker."""


class _Location(BaseModel):
    row: int | None = None
    column: int | None = None


class _Diagnostic(BaseModel):
    code: str | None = None
    message: str
    location: _Location | None = None


_DIAGNOSTICS = TypeAdapter(list[_Diagnostic])


class RuffValidator:
    """Non-blocking check: findings make the result PARTIAL, not FAILURE."""

    name = "ruff"

    def __init__(
        self,
        *,
        rules: Sequence[str] = DEFAULT_RULES,
        target_version: str = "py314",
        timeout_seconds: float = 20.0,
    ) -> None:
        self._rules = rules
        self._target_version = target_version
        self._timeout_seconds = timeout_seconds

    async def validate(self, code: str) -> ValidationResult:
        stdout = await self._run(code)
        diagnostics = _DIAGNOSTICS.validate_json(stdout or b"[]")
        messages = tuple(
            ValidationMessage(
                message=d.message,
                code=d.code,
                line=d.location.row if d.location else None,
                column=d.location.column if d.location else None,
            )
            for d in diagnostics
        )
        return ValidationResult(
            results=(
                ValidatorResult(
                    validator=self.name, success=not messages, blocking=False, messages=messages
                ),
            )
        )

    async def _run(self, code: str) -> bytes:
        command = (
            find_ruff_bin(),
            "check",
            "--isolated",  # ignore any pyproject/ruff.toml around the process
            "--no-cache",
            "--output-format=json",
            f"--target-version={self._target_version}",
            f"--select={','.join(self._rules)}",
            "--stdin-filename=generated_module.py",
            "-",
        )
        # subprocess.run in a worker thread instead of asyncio subprocesses: works on every
        # event loop (Windows SelectorEventLoop has no subprocess support) and never blocks it.
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            input=code.encode(),
            capture_output=True,
            timeout=self._timeout_seconds,
            check=False,
        )
        # 0 = clean, 1 = violations found, anything else = Ruff itself failed
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.decode(errors="replace")
            raise AppError(f"Ruff failed to run: {stderr}", returncode=completed.returncode)
        return completed.stdout
