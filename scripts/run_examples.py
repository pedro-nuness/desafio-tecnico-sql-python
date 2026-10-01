"""Runs the pipeline over the challenge annexes (B-F) and writes the results to disk.

Goes through ModernizeRoutine, the same use case behind POST /modernize, so every run is
also persisted in `modernization_history`. Requires a migrated PostgreSQL (DATABASE_URL) and
the LLM and EVALUATION_DATABASE_URL configured in `.env`.

    uv run python -m scripts.run_examples

Output: examples/results/<procedure>/{generated.py,report.json} + examples/results/SUMMARY.md
"""

import asyncio
import json
from pathlib import Path

from app.core.bootstrap import build_container
from app.core.config.settings import Settings
from app.features.modernization.domain import Modernization
from app.features.modernization.evaluation.domain import Evaluation, EvaluationSummary
from app.features.modernization.use_cases import (
    EvaluateCommand,
    EvaluateModernization,
    ModernizeCommand,
    ModernizeRoutine,
)
from app.shared.errors import AppError

EXAMPLES = Path(__file__).parents[1] / "examples"
PROCEDURES = EXAMPLES / "procedures"
RESULTS = EXAMPLES / "results"


async def main() -> None:
    settings = Settings()
    if settings.evaluation_database_url is None:
        raise AppError("Set EVALUATION_DATABASE_URL before generating the examples")
    schema = (EXAMPLES / "schema.sql").read_text(encoding="utf-8")
    sources = sorted(PROCEDURES.glob("*.sql"))
    container = build_container(settings)
    try:
        modernize = await container.get(ModernizeRoutine)
        # Independent executions: run concurrently, like concurrent POST /modernize calls.
        runs = await asyncio.gather(
            *(
                modernize.execute(ModernizeCommand(path.read_text(encoding="utf-8"), schema))
                for path in sources
            )
        )
        evaluate = await container.get(EvaluateModernization)
        evaluations = [await evaluate.execute(EvaluateCommand(run.id)) for run in runs]
    finally:
        await container.close()

    named = [(path.stem, run) for path, run in zip(sources, runs, strict=True)]
    for (name, run), evaluation in zip(named, evaluations, strict=True):
        _write_run(RESULTS / name, run, evaluation)
    summary = _summary(settings, named, evaluations)
    (RESULTS / "SUMMARY.md").write_text(summary, encoding="utf-8")
    print(summary)


def _write_run(directory: Path, run: Modernization, evaluation: Evaluation) -> None:
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
    (directory / "evaluation.json").write_text(
        json.dumps(evaluation.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _summary(
    settings: Settings,
    runs: list[tuple[str, Modernization]],
    evaluations: list[Evaluation],
) -> str:
    metrics = EvaluationSummary(evaluations=tuple(evaluations))
    lines = [
        "# Resultados — Anexos B a F",
        "",
        f"Modelo: `{settings.llm_provider.value}/{settings.llm_model}` · "
        "schema do Anexo A enviado como contexto · gerado por `scripts/run_examples.py`.",
        "",
        f"Prompt: `{', '.join(sorted({e.prompt_version or 'unknown' for e in evaluations}))}`. "
        f"Equivalência: **{sum(e.equivalent for e in evaluations)}/{len(evaluations)} rotinas "
        f"({metrics.equivalence_rate:.0%})**; "
        f"**{sum(e.cases_passed for e in evaluations)}/{sum(e.cases_total for e in evaluations)} "
        f"casos ({metrics.case_pass_rate:.1%})**. "
        f"Só holdout (casos nunca mostrados ao LLM): "
        f"**{sum(e.holdout_equivalent for e in evaluations)}/{len(evaluations)} rotinas, "
        f"{sum(e.holdout_passed for e in evaluations)}/"
        f"{sum(len(e.holdout_cases) for e in evaluations)} casos "
        f"({metrics.holdout_case_pass_rate:.1%})**. "
        f"Validade estática (AST): {metrics.static_valid_rate:.0%}; "
        f"conclusão: {metrics.completion_rate:.0%}.",
        "",
        "| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff"
        " | tokens in/out (última) | duração total | equivalência | holdout |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (name, run), evaluation in zip(runs, evaluations, strict=True):
        report = run.report
        generation = report.generation
        analysis = report.semantic_analysis
        validation = report.validation
        risks = sorted({risk.code for risk in analysis.risks}) if analysis else []
        lint = (
            next((v for v in validation.results if v.validator == "ruff"), None)
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
                        if generation
                        else "—"
                    ),
                    ", ".join(risks) or "—",
                    "—" if lint is None else ("ok" if lint.success else f"{len(lint.messages)}"),
                    (
                        f"{generation.input_tokens} / {generation.output_tokens}"
                        if generation
                        else "—"
                    ),
                    f"{(run.updated_at - run.created_at).total_seconds():.1f}s",
                    f"[{evaluation.cases_passed}/{evaluation.cases_total} "
                    f"({evaluation.score:.0%})]({name}/evaluation.json)",
                    f"{evaluation.holdout_passed}/{len(evaluation.holdout_cases)}",
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
    failures = [
        f"- `{name}` · {case.name}{' (holdout)' if case.holdout else ''}: {case.detail}"
        for (name, _), evaluation in zip(runs, evaluations, strict=True)
        for case in evaluation.cases
        if not case.passed
    ]
    if failures:
        lines.extend(["", "## Divergências comportamentais", "", *failures])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
