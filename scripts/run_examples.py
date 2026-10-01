"""Runs the pipeline over the challenge annexes (B-F) and writes the results to disk.

Goes through ModernizationService, the same use case behind POST /modernize, so every run is
also persisted in `modernization_history`. Requires a migrated PostgreSQL (DATABASE_URL) and
the LLM configured in `.env`.

    uv run python -m scripts.run_examples

Output: examples/results/<procedure>/{generated.py,report.json} + examples/results/SUMMARY.md
"""

import asyncio
import json
from pathlib import Path

from app.core.bootstrap import build_container
from app.core.config.settings import Settings
from app.features.modernization.domain.models.modernization import Modernization

EXAMPLES = Path(__file__).parents[1] / "examples"
PROCEDURES = EXAMPLES / "procedures"
RESULTS = EXAMPLES / "results"


async def main() -> None:
    settings = Settings()
    schema = (EXAMPLES / "schema.sql").read_text(encoding="utf-8")
    sources = sorted(PROCEDURES.glob("*.sql"))
    container = build_container(settings)
    try:
        # Independent executions: run concurrently, like concurrent POST /modernize calls.
        runs = await asyncio.gather(
            *(
                container.modernization_service.modernize(path.read_text(encoding="utf-8"), schema)
                for path in sources
            )
        )
    finally:
        await container.aclose()

    named = [(path.stem, run) for path, run in zip(sources, runs, strict=True)]
    for name, run in named:
        _write_run(RESULTS / name, run)
    summary = _summary(settings, named)
    (RESULTS / "SUMMARY.md").write_text(summary, encoding="utf-8")
    print(summary)


def _write_run(directory: Path, run: Modernization) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if run.generated_code is not None:
        (directory / "generated.py").write_text(run.generated_code, encoding="utf-8")
    payload = {
        "execution_id": str(run.id),
        "status": run.status.value,
        "created_at": run.created_at.isoformat(),
        "report": run.report.model_dump(mode="json"),
    }
    (directory / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _summary(settings: Settings, runs: list[tuple[str, Modernization]]) -> str:
    lines = [
        "# Resultados — Anexos B a F",
        "",
        f"Modelo: `{settings.llm_provider.value}/{settings.llm_model}` · "
        "schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.",
        "",
        "| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff"
        " | tokens in/out (última) | duração total |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, run in runs:
        report = run.report
        generation = report.generation
        analysis = report.semantic_analysis
        validation = report.validation
        risks = sorted({risk.code for risk in analysis.risks}) if analysis else []
        lint = (
            next((v for v in validation.validators if v.validator == "ruff"), None)
            if validation
            else None
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[{name}]({name}/)",
                    run.status.value,
                    str(generation.attempt) if generation and generation.attempt else "—",
                    (
                        f"{generation.strategy} / {generation.recommended_strategy}"
                        if generation and generation.success
                        else "—"
                    ),
                    ", ".join(risks) or "—",
                    "—" if lint is None else ("ok" if lint.success else f"{len(lint.messages)}"),
                    (
                        f"{generation.input_tokens} / {generation.output_tokens}"
                        if generation and generation.success
                        else "—"
                    ),
                    f"{(run.updated_at - run.created_at).total_seconds():.1f}s",
                ]
            )
            + " |"
        )
    errors = [
        f"- `{name}` · `{error.step}`: {error.message}"
        for name, run in runs
        for error in run.report.errors
    ]
    if errors:
        lines.extend(["", "## Erros", "", *errors])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
