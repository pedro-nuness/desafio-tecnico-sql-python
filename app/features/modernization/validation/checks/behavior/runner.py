"""Subprocess entry point of the behavioral-equivalence harness.

Generated code is executed here, never in the server process: a crash, a hang or leaked
module state stay contained. The request comes as JSON on stdin; the result is the last stdout
line prefixed with RESULT_MARKER (the generated code may print too).

    python -m app.features.modernization.validation.checks.behavior.runner < request.json
"""

import asyncio
import json
import sys
from pathlib import Path

from app.features.modernization.parsing.domain import Parameter
from app.features.modernization.validation.checks.behavior.harness import (
    RESULT_MARKER,
    BehavioralEquivalence,
)


async def main() -> None:
    request = json.loads(sys.stdin.read())
    harness = BehavioralEquivalence(
        request["database_url"],
        Path(request["dataset_file"]),
        case_timeout_seconds=request["case_timeout_seconds"],
        isolate=False,
    )
    try:
        cases = await harness.run(
            routine=request["routine"],
            source_code=request["source_code"],
            code=request["code"],
            parameters=tuple(Parameter.model_validate(p) for p in request["parameters"]),
            include_holdout=request["include_holdout"],
        )
    finally:
        await harness.close()
    payload = None if cases is None else [case.model_dump(mode="json") for case in cases]
    print(RESULT_MARKER + json.dumps(payload), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
