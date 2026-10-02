"""Runs the pipeline over the challenge annexes (B-F) and writes the results to disk.

Goes through ModernizeRoutine, the same use case behind POST /modernize, so every run is
also persisted in `modernization_history`. Requires a migrated PostgreSQL (DATABASE_URL) and
the LLM and EVALUATION_DATABASE_URL configured in `.env`.

Usage:
    uv run python -m scripts.run_examples
    uv run python -m scripts.run_examples --with-generated-cases
    uv run python -m scripts.run_examples --without-generated-cases
    uv run python -m scripts.run_examples -p b_fn_saldo_cliente
    uv run python -m scripts.run_examples -p b,c --tag "teste-rapido"
    uv run python -m scripts.run_examples --history
    uv run python -m scripts.run_examples --detail

Comparing the holdout rate of the two modes measures what the generated cases add.

Output (every run is kept in its own snapshot, nothing is overwritten):
    examples/results/HISTORY.md
    examples/results/history.jsonl
    examples/results/history/run_<id>_{tag}/{run.json,SUMMARY.md}
    examples/results/history/run_<id>_{tag}/<procedure>/
        {procedure.sql,payload.json,generated.py,report.json,evaluation.json}
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from app.core.bootstrap import build_container
from app.core.config.settings import Settings
from app.features.modernization.domain import Modernization
from app.features.modernization.evaluation.domain import Evaluation, EvaluationSummary
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.use_cases import (
    EvaluateCommand,
    EvaluateModernization,
    ModernizeCommand,
    ModernizeRoutine,
)
from app.features.modernization.validation.checks.behavior.dataset import Dataset
from app.shared.errors import AppError

EXAMPLES = Path(__file__).parents[1] / "examples"
PROCEDURES = EXAMPLES / "procedures"
RESULTS = EXAMPLES / "results"
HISTORY_FILE = RESULTS / "history.jsonl"
HISTORY_MD = RESULTS / "HISTORY.md"
HISTORY_DIR = RESULTS / "history"


# --------------------------------------------------------------------------- History Tracking


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def _format_duration(record: dict[str, Any]) -> str:
    seconds = record.get("duration_seconds")
    return "—" if seconds is None else f"{seconds:.1f}s"


def _load_history(history_file: Path = HISTORY_FILE) -> list[dict[str, Any]]:
    if not history_file.exists():
        return []
    records = []
    with history_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _update_history_md(records: list[dict[str, Any]], history_md_path: Path = HISTORY_MD) -> None:
    lines = [
        "# Historico de Execucoes e Tracking de Metricas",
        "",
        "Rastreamento consolidado das execucoes do pipeline (`scripts/run_examples.py`).",
        "Acompanha a evolucao das taxas de equivalencia comportamental, holdout, tempo e tokens.",
        "",
        (
            "| Run ID | Data (UTC) | Tag | Modelo | Prompt | Casos LLM | Equivalencia | "
            "Casos (Geral) | Holdout | Tokens (In / Out) | Duracao |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reversed(records):  # Most recent first
        run_id = r.get("run_id", "—")
        ts = r.get("timestamp", "")[:16].replace("T", " ")
        tag = r.get("tag") or "default"
        model = r.get("model", "—")
        prompt = ", ".join(r.get("prompt_versions", [])) or "—"
        cases_mode = "Ligado" if r.get("generate_cases", True) else "Desligado"
        eq_str = (
            f"**{r.get('routines_equivalent', 0)}/{r.get('routines_count', 0)} "
            f"({r.get('equivalence_rate', 0.0):.0%})**"
        )
        cases_str = (
            f"{r.get('cases_passed', 0)}/{r.get('cases_total', 0)} "
            f"({r.get('case_pass_rate', 0.0):.1%})"
        )
        h_str = (
            f"{r.get('holdout_passed', 0)}/{r.get('holdout_total', 0)} "
            f"({r.get('holdout_pass_rate', 0.0):.1%})"
        )
        tok_str = (
            f"{_format_tokens(r.get('tokens_in', 0))} / {_format_tokens(r.get('tokens_out', 0))}"
        )
        dur_str = _format_duration(r)
        row = (
            f"| `{run_id}` | {ts} | `{tag}` | `{model}` | `{prompt}` | {cases_mode} | "
            f"{eq_str} | {cases_str} | {h_str} | {tok_str} | {dur_str} |"
        )
        lines.append(row)
    lines.append("")
    history_md_path.write_text("\n".join(lines), encoding="utf-8")


def _lines(text: str | None) -> list[str] | None:
    """SQL as a list of lines, so it reads like the source in the JSON instead of "\\n"s."""
    return None if text is None else text.splitlines()


def _payload(command: ModernizeCommand) -> dict[str, Any]:
    """The command sent to ModernizeRoutine, as JSON (behavior holds only the dev cases)."""
    behavior = None
    if command.behavior is not None:
        behavior = command.behavior.model_dump(mode="json")
        behavior["setup_sql"] = _lines(command.behavior.setup_sql)
    return {
        "source_code": _lines(command.source_code),
        "schema_context": _lines(command.schema_context),
        "behavior": behavior,
        "generate_cases": command.generate_cases,
    }


def _snapshot_dir(results_dir: Path, run_id: str, tag: str | None) -> Path:
    return results_dir / "history" / f"run_{run_id}_{tag or 'run'}"


def _record_history(
    settings: Settings,
    runs: list[tuple[str, Modernization]],
    evaluations: list[Evaluation],
    commands: list[ModernizeCommand],
    *,
    generate_cases: bool,
    tag: str | None = None,
    duration_seconds: float = 0.0,
    results_dir: Path = RESULTS,
) -> dict[str, Any]:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    now_iso = datetime.now(UTC).isoformat()
    metrics = EvaluationSummary(evaluations=tuple(evaluations))

    tokens_in = sum(
        (r.report.code_generation.input_tokens if r.report.code_generation else 0) for _, r in runs
    )
    tokens_out = sum(
        (r.report.code_generation.output_tokens if r.report.code_generation else 0) for _, r in runs
    )
    prompt_versions = sorted({e.prompt_version or "unknown" for e in evaluations})

    procedures_detail = {}
    for (name, r), ev in zip(runs, evaluations, strict=True):
        gen = r.report.code_generation
        dur = round((r.updated_at - r.created_at).total_seconds(), 1)
        procedures_detail[name] = {
            "status": r.status.value,
            "strategy": gen.strategy if gen else None,
            "recommended_strategy": gen.recommended_strategy if gen else None,
            "attempt": gen.attempt if gen else None,
            "score": ev.score,
            "cases_passed": ev.cases_passed,
            "cases_total": ev.cases_total,
            "holdout_passed": ev.holdout_passed,
            "holdout_total": len(ev.holdout_cases),
            "input_tokens": gen.input_tokens if gen else 0,
            "output_tokens": gen.output_tokens if gen else 0,
            "duration_seconds": dur,
        }

    provider_name = (
        settings.llm_provider.value
        if hasattr(settings.llm_provider, "value")
        else str(settings.llm_provider)
    )

    record = {
        "run_id": run_id,
        "timestamp": now_iso,
        "tag": tag or "default",
        "provider": provider_name,
        "model": settings.llm_model,
        "prompt_versions": prompt_versions,
        "generate_cases": generate_cases,
        "routines_count": len(evaluations),
        "routines_equivalent": sum(e.equivalent for e in evaluations),
        "equivalence_rate": round(metrics.equivalence_rate, 4),
        "cases_passed": sum(e.cases_passed for e in evaluations),
        "cases_total": sum(e.cases_total for e in evaluations),
        "case_pass_rate": round(metrics.case_pass_rate, 4),
        "holdout_passed": sum(e.holdout_passed for e in evaluations),
        "holdout_total": sum(len(e.holdout_cases) for e in evaluations),
        "holdout_pass_rate": round(metrics.holdout_case_pass_rate, 4),
        "static_valid_rate": round(metrics.static_valid_rate, 4),
        "completion_rate": round(metrics.completion_rate, 4),
        "duration_seconds": round(duration_seconds, 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "procedures": procedures_detail,
    }

    results_dir.mkdir(parents=True, exist_ok=True)
    history_file = results_dir / "history.jsonl"
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    records = _load_history(history_file)
    _update_history_md(records, results_dir / "HISTORY.md")

    snapshot_dir = _snapshot_dir(results_dir, run_id, tag)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "run.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary_content = _summary(settings, runs, evaluations, generate_cases=generate_cases, tag=tag)
    (snapshot_dir / "SUMMARY.md").write_text(summary_content, encoding="utf-8")

    # What each routine received and returned in this run.
    for (name, run), evaluation, command in zip(runs, evaluations, commands, strict=True):
        procedure_dir = snapshot_dir / name
        _write_run(procedure_dir, run, evaluation)
        (procedure_dir / "procedure.sql").write_text(command.source_code, encoding="utf-8")
        (procedure_dir / "payload.json").write_text(
            json.dumps(_payload(command), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    return record


def _display_history(console: Console, results_dir: Path = RESULTS) -> None:
    history_file = results_dir / "history.jsonl"
    records = _load_history(history_file)
    if not records:
        console.print("[yellow]Nenhuma execucao encontrada no historico.[/]")
        console.print(
            "[dim]Execute: [bold]uv run python -m scripts.run_examples[/] "
            "para criar o primeiro registro.[/]"
        )
        return

    table = Table(
        title="[bold cyan]Historico de Execucoes e Tracking de Metricas[/]",
        box=box.ROUNDED,
        header_style="bold magenta",
        title_style="bold cyan",
    )
    table.add_column("Run ID", style="dim", no_wrap=True)
    table.add_column("Data (UTC)", style="white", no_wrap=True)
    table.add_column("Tag", style="cyan")
    table.add_column("Modelo", style="blue")
    table.add_column("Prompt", style="dim")
    table.add_column("Casos LLM", justify="center")
    table.add_column("Equivalencia", justify="center")
    table.add_column("Casos Geral", justify="center")
    table.add_column("Holdout", justify="center")
    table.add_column("Tokens (In/Out)", justify="right")
    table.add_column("Duracao", justify="right")

    for r in records:
        eq_rate = r.get("equivalence_rate", 0.0)
        eq_color = "green" if eq_rate >= 0.8 else ("yellow" if eq_rate > 0 else "red")
        eq_text = (
            f"[{eq_color}]{r.get('routines_equivalent', 0)}/{r.get('routines_count', 0)} "
            f"({eq_rate:.0%})[/]"
        )

        pass_rate = r.get("case_pass_rate", 0.0)
        pass_color = "green" if pass_rate >= 0.8 else ("yellow" if pass_rate > 0 else "red")
        pass_text = (
            f"[{pass_color}]{r.get('cases_passed', 0)}/{r.get('cases_total', 0)} "
            f"({pass_rate:.1%})[/]"
        )

        h_rate = r.get("holdout_pass_rate", 0.0)
        h_color = "green" if h_rate >= 0.8 else ("yellow" if h_rate > 0 else "red")
        h_text = (
            f"[{h_color}]{r.get('holdout_passed', 0)}/{r.get('holdout_total', 0)} ({h_rate:.1%})[/]"
        )

        cases_mode = "[green]ON[/]" if r.get("generate_cases", True) else "[dim]OFF[/]"
        tok = f"{_format_tokens(r.get('tokens_in', 0))} / {_format_tokens(r.get('tokens_out', 0))}"
        dur = _format_duration(r)

        table.add_row(
            r.get("run_id", "—"),
            r.get("timestamp", "")[:16].replace("T", " "),
            r.get("tag") or "—",
            r.get("model", "—"),
            ", ".join(r.get("prompt_versions", [])) or "—",
            cases_mode,
            eq_text,
            pass_text,
            h_text,
            tok,
            dur,
        )

    console.print(table)
    total_msg = f"[dim]Total de execucoes: [bold]{len(records)}[/] · Log: {history_file}[/]\n"
    console.print(total_msg)


# --------------------------------------------------------------------------- Artifacts Writing


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
    *,
    generate_cases: bool,
    tag: str | None = None,
) -> str:
    metrics = EvaluationSummary(evaluations=tuple(evaluations))
    tag_info = f" · tag: `{tag}`" if tag else ""
    lines = [
        "# Resultados — Anexos B a F",
        "",
        f"Modelo: `{settings.llm_provider.value}/{settings.llm_model}` · "
        "schema do Anexo A enviado como contexto · gerado por "
        f"`scripts/run_examples.py`{tag_info}.",
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
        "Casos gerados pelo LLM (somados aos dev no pipeline): "
        + ("**ligado**." if generate_cases else "**desligado** (`--without-generated-cases`)."),
        "",
        "| procedure | status | tentativas | estratégia (LLM / recomendada) | riscos | ruff"
        " | tokens in/out (última) | duração total | casos gerados (mantidos / descartados)"
        " | equivalência | holdout |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (name, run), evaluation in zip(runs, evaluations, strict=True):
        report = run.report
        generation = report.code_generation
        analysis = report.semantic_analysis
        validation = report.validation
        cases = report.case_generation
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
                    f"{len(cases.kept)} / {len(cases.discarded)}" if cases else "—",
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


# --------------------------------------------------------------------------- Screen Reports


def _print_banner(
    console: Console,
    settings: Settings,
    routine_names: list[str],
    *,
    generate_cases: bool,
    tag: str | None,
) -> None:
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    cases_style = "bold green" if generate_cases else "bold yellow"
    cases_text = (
        "Ativado (LLM gera casos sinteticos)"
        if generate_cases
        else "Desativado (somente casos dev)"
    )

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column(style="bold white")

    provider_name = (
        settings.llm_provider.value
        if hasattr(settings.llm_provider, "value")
        else str(settings.llm_provider)
    )

    grid.add_row(
        "Provedor / Modelo:",
        f"[bold cyan]{provider_name}[/] / [bold magenta]{settings.llm_model}[/]",
    )
    grid.add_row(
        "Rotinas Alvo:",
        f"[bold green]{len(routine_names)}[/] rotina(s) ({', '.join(routine_names)})",
    )
    grid.add_row(
        "Schema de Entrada:",
        "[cyan]Anexo A (examples/schema.sql) enviado como contexto[/]",
    )
    grid.add_row("Casos Sinteticos:", f"[{cases_style}]{cases_text}[/]")
    grid.add_row("Modo de Execucao:", "[cyan]Sequencial[/] (1 rotina por vez)")
    if tag:
        grid.add_row("Tag da Execucao:", f"[bold blue]{tag}[/]")
    grid.add_row("Inicio da Execucao:", f"[dim]{now_str}[/]")

    banner = Panel(
        grid,
        title="[bold yellow]MODERNIZACAO PL/pgSQL -> PYTHON 3.14 (LANGGRAPH PIPELINE)[/]",
        subtitle="[dim]Pipeline de Modernizacao e Test Harness de Equivalencia Comportamental[/]",
        border_style="bright_blue",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    console.print(banner)
    console.print()


def _print_screen_report(
    console: Console,
    settings: Settings,
    runs: list[tuple[str, Modernization]],
    evaluations: list[Evaluation],
    *,
    snapshot_dir: Path,
    generate_cases: bool,
    tag: str | None = None,
    total_duration: float = 0.0,
    show_detail: bool = False,
) -> None:
    metrics = EvaluationSummary(evaluations=tuple(evaluations))

    eq_rate = metrics.equivalence_rate
    eq_color = "bold green" if eq_rate >= 0.8 else ("bold yellow" if eq_rate > 0 else "bold red")

    case_rate = metrics.case_pass_rate
    case_color = (
        "bold green" if case_rate >= 0.8 else ("bold yellow" if case_rate > 0 else "bold red")
    )

    h_rate = metrics.holdout_case_pass_rate
    h_color = "bold green" if h_rate >= 0.8 else ("bold yellow" if h_rate > 0 else "bold red")

    total_tok_in = sum(
        (r.report.code_generation.input_tokens if r.report.code_generation else 0) for _, r in runs
    )
    total_tok_out = sum(
        (r.report.code_generation.output_tokens if r.report.code_generation else 0) for _, r in runs
    )
    total_tok = total_tok_in + total_tok_out

    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="bold white", justify="left")
    grid.add_column(style="bold white", justify="left")

    eq_str = (
        f"Equivalencia Global: [{eq_color}]{sum(e.equivalent for e in evaluations)}/"
        f"{len(evaluations)} ({eq_rate:.0%})[/]"
    )
    cases_tot_str = (
        f"Casos de Teste (Total): [{case_color}]{sum(e.cases_passed for e in evaluations)}/"
        f"{sum(e.cases_total for e in evaluations)} ({case_rate:.1%})[/]"
    )
    grid.add_row(eq_str, cases_tot_str)

    h_str = (
        f"Holdout (Zero-Shot):   [{h_color}]{sum(e.holdout_passed for e in evaluations)}/"
        f"{sum(len(e.holdout_cases) for e in evaluations)} ({h_rate:.1%})[/]"
    )
    grid.add_row(
        h_str,
        f"Validade Estatica (AST): [bold green]{metrics.static_valid_rate:.0%}[/]",
    )
    grid.add_row(
        f"Taxa de Conclusao:     [bold green]{metrics.completion_rate:.0%}[/]",
        f"Duracao Total:          [bold cyan]{total_duration:.1f}s[/]",
    )
    tok_fmt = (
        f"[dim]{total_tok_in:,} in / {total_tok_out:,} out[/] ([bold cyan]{total_tok:,} total[/])"
    )
    cases_mode_str = (
        f"[{'green' if generate_cases else 'yellow'}]"
        f"{'Ativado' if generate_cases else 'Desativado'}[/]"
    )
    grid.add_row(
        f"Consumo de Tokens:     {tok_fmt}",
        f"Casos Sinteticos:       {cases_mode_str}",
    )

    scorecard = Panel(
        grid,
        title=f"[bold green]Scorecard Consolidado - {settings.llm_model}[/]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    console.print()
    console.print(scorecard)
    console.print()

    table = Table(
        title="[bold white]Resultados Detalhados por Rotina[/]",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="blue",
    )
    table.add_column("Rotina", style="bold white")
    table.add_column("Status", justify="center")
    table.add_column("Tent.", justify="center")
    table.add_column("Estrategia (LLM / Rec)")
    table.add_column("Riscos")
    table.add_column("Ruff", justify="center")
    table.add_column("Tokens (In/Out)", justify="right")
    table.add_column("Duracao", justify="right")
    table.add_column("Casos LLM", justify="center")
    table.add_column("Equivalencia", justify="center")
    table.add_column("Holdout", justify="center")

    for (name, run), evaluation in zip(runs, evaluations, strict=True):
        report = run.report
        generation = report.code_generation
        analysis = report.semantic_analysis
        validation = report.validation
        cases = report.case_generation

        status_str = {
            "success": "[bold green]SUCCESS[/]",
            "partial": "[bold yellow]PARTIAL[/]",
            "failure": "[bold red]FAILURE[/]",
        }.get(run.status.value, run.status.value)

        attempts_str = str(generation.attempt) if generation and generation.attempt else "—"
        if generation and generation.attempt and generation.attempt > 1:
            attempts_str = f"[bold yellow]{attempts_str}[/]"

        if generation:
            strat_color = (
                "green" if generation.strategy == generation.recommended_strategy else "yellow"
            )
            strat_str = (
                f"[{strat_color}]{generation.strategy}[/] / "
                f"[dim]{generation.recommended_strategy}[/]"
            )
        else:
            strat_str = "—"

        risks = sorted({risk.code for risk in analysis.risks}) if analysis else []
        risks_str = " ".join(f"[bold yellow]{r}[/]" for r in risks) if risks else "[dim]—[/dim]"

        lint = (
            next((v for v in validation.results if v.validator == "ruff"), None)
            if validation
            else None
        )
        if lint is None:
            lint_str = "[dim]—[/dim]"
        elif lint.success:
            lint_str = "[green]ok[/]"
        else:
            lint_str = f"[bold red]{len(lint.messages)} msgs[/]"

        if generation:
            tokens_str = f"{generation.input_tokens:,} / {generation.output_tokens:,}"
        else:
            tokens_str = "[dim]—[/dim]"

        dur = (run.updated_at - run.created_at).total_seconds()
        dur_str = f"{dur:.1f}s"

        cases_str = f"{len(cases.kept)} / {len(cases.discarded)}" if cases else "[dim]—[/dim]"

        eq_score_color = (
            "green" if evaluation.equivalent else ("yellow" if evaluation.score > 0 else "red")
        )
        eq_cell = (
            f"[{eq_score_color}]{evaluation.cases_passed}/{evaluation.cases_total} "
            f"({evaluation.score:.0%})[/]"
        )

        h_cases = evaluation.holdout_cases
        h_color = (
            "green"
            if evaluation.holdout_equivalent
            else ("yellow" if evaluation.holdout_passed > 0 else "red")
        )
        h_cell = (
            f"[{h_color}]{evaluation.holdout_passed}/{len(h_cases)}[/]"
            if h_cases
            else "[dim]—[/dim]"
        )

        table.add_row(
            f"[bold cyan]{name}[/]",
            status_str,
            attempts_str,
            strat_str,
            risks_str,
            lint_str,
            tokens_str,
            dur_str,
            cases_str,
            eq_cell,
            h_cell,
        )

    console.print(table)
    console.print()

    failures = [
        (name, case)
        for (name, _), evaluation in zip(runs, evaluations, strict=True)
        for case in evaluation.cases
        if not case.passed
    ]
    if failures:
        div_table = Table(
            title="[bold red]Divergencias Comportamentais Detectadas[/]",
            box=box.ROUNDED,
            border_style="red",
            header_style="bold red",
        )
        div_table.add_column("Rotina", style="bold cyan")
        div_table.add_column("Caso de Teste", style="white")
        div_table.add_column("Escopo", justify="center")
        div_table.add_column("Detalhe / Erro", style="yellow")
        for name, case in failures:
            scope = "[bold magenta]Holdout[/]" if case.holdout else "[bold cyan]Dev[/]"
            div_table.add_row(name, case.name, scope, case.detail)
        console.print(div_table)
        console.print()

    errors = [(name, error) for name, run in runs for error in run.report.errors]
    if errors:
        err_table = Table(
            title="[bold red]Erros Ocorridos no Pipeline[/]",
            box=box.ROUNDED,
            border_style="red",
            header_style="bold red",
        )
        err_table.add_column("Rotina", style="bold cyan")
        err_table.add_column("Etapa", style="yellow")
        err_table.add_column("Mensagem", style="red")
        for name, error in errors:
            err_table.add_row(name, str(error.step or "pipeline"), error.message)
        console.print(err_table)
        console.print()

    if show_detail:
        detail_table = Table(
            title="[bold white]Detalhamento Completo dos Casos de Teste[/]",
            box=box.ROUNDED,
            border_style="cyan",
            header_style="bold cyan",
        )
        detail_table.add_column("Rotina", style="bold cyan")
        detail_table.add_column("Caso", style="white")
        detail_table.add_column("Tipo", justify="center")
        detail_table.add_column("Status", justify="center")
        detail_table.add_column("Original", style="dim")
        detail_table.add_column("Gerado", style="dim")
        for (name, _), evaluation in zip(runs, evaluations, strict=True):
            for c in evaluation.cases:
                st = "[bold green]PASS[/]" if c.passed else "[bold red]FAIL[/]"
                tp = "[magenta]Holdout[/]" if c.holdout else "[cyan]Dev[/]"
                detail_table.add_row(name, c.name, tp, st, c.original or "—", c.generated or "—")
        console.print(detail_table)
        console.print()

    footer_text = (
        f"  Snapshot da Execucao:   [cyan]{snapshot_dir}[/]\n"
        f"  Historico (Tracking):   [cyan]{RESULTS / 'HISTORY.md'}[/]\n"
        f"  Log Estruturado:        [cyan]{RESULTS / 'history.jsonl'}[/]"
    )
    console.print(
        Panel(
            footer_text,
            title="[bold green]Artefatos e Tracking[/]",
            border_style="green",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )
    console.print()


# --------------------------------------------------------------------------- CLI & Runner


def _expand_procedure_filters(raw_filters: list[str] | None) -> list[str]:
    if not raw_filters:
        return []
    result = []
    for item in raw_filters:
        for piece in item.split(","):
            piece = piece.strip()
            if piece:
                result.append(piece)
    return result


def _matches_filter(stem: str, f: str) -> bool:
    stem_lower = stem.lower()
    f_lower = f.lower().strip()
    if stem_lower == f_lower:
        return True
    if stem_lower.startswith(f"{f_lower}_") or stem_lower.startswith(f_lower):
        return True
    return len(f_lower) >= 3 and f_lower in stem_lower


def _filter_procedures(sources: list[Path], raw_filters: list[str] | None) -> list[Path]:
    filters = _expand_procedure_filters(raw_filters)
    if not filters:
        return sources
    matched = []
    for path in sources:
        if any(_matches_filter(path.stem, f) for f in filters):
            matched.append(path)
    return matched


def _prompt_pre_run_options(
    console: Console,
    all_sources: list[Path],
    cli_procedures: list[str] | None,
    cli_without_cases: bool,
    cli_with_cases: bool,
) -> tuple[list[Path], bool]:
    """Provides interactive choice for generated-cases and procedures before running."""
    # 1. Decide generated cases mode
    if cli_without_cases:
        generate_cases = False
    elif cli_with_cases:
        generate_cases = True
    elif sys.stdin.isatty():
        menu_text = (
            "[bold cyan]Opcoes de Execucao da Modernizacao[/]\n\n"
            "[bold]Modo de Casos de Teste:[/] (Ambos enviam o schema do Anexo A)\n"
            "  [bold green][1][/] Com casos gerados pelo LLM (dev + sinteticos da IA)\n"
            "  [bold yellow][2][/] Sem casos gerados pelo LLM (somente dev - mais rapido)\n\n"
            "[dim]Dica: o modo sem casos gerados economiza chamadas de IA e testes.[/]"
        )
        console.print(
            Panel(
                menu_text,
                title="[bold white]Configuracao[/]",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )
        try:
            choice = input("Escolha o modo [1/2] (Enter para 1): ").strip()
            generate_cases = choice != "2"
        except EOFError, KeyboardInterrupt:
            generate_cases = True
    else:
        generate_cases = True

    # 2. Decide procedures to run
    if cli_procedures:
        sources = _filter_procedures(all_sources, cli_procedures)
    elif sys.stdin.isatty() and not cli_with_cases and not cli_without_cases:
        console.print()
        console.print("[bold]Rotinas disponiveis (Anexos B a F):[/]")
        console.print("  [0] Todas as rotinas (Anexos B a F)")
        for idx, src in enumerate(all_sources, 1):
            console.print(f"  [{idx}] {src.stem}")
        try:
            p_choice = input(
                "Selecione as rotinas (ex: 0 para todas, ou 1,2) [padrao: 0]: "
            ).strip()
            if not p_choice or p_choice == "0":
                sources = all_sources
            else:
                selected_indices = set()
                matched_sources = []
                for part in p_choice.split(","):
                    part = part.strip()
                    if part.isdigit():
                        selected_indices.add(int(part))
                    elif part:
                        matched_sources.extend(
                            [s for s in all_sources if _matches_filter(s.stem, part)]
                        )
                if selected_indices:
                    matched_sources.extend(
                        [src for idx, src in enumerate(all_sources, 1) if idx in selected_indices]
                    )
                sources = sorted(list(set(matched_sources)), key=lambda s: s.stem)
                if not sources:
                    sources = all_sources
        except EOFError, KeyboardInterrupt:
            sources = all_sources
    else:
        sources = all_sources

    return sources, generate_cases


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline de Modernizacao PL/pgSQL -> Python 3.14 (Anexos B a F)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos de uso:
  uv run python -m scripts.run_examples
  uv run python -m scripts.run_examples --with-generated-cases
  uv run python -m scripts.run_examples --without-generated-cases
  uv run python -m scripts.run_examples -p b_fn_saldo_cliente
  uv run python -m scripts.run_examples -p b,c --tag "teste-rapido"
  uv run python -m scripts.run_examples --history
  uv run python -m scripts.run_examples --detail
""",
    )
    parser.add_argument(
        "--with-generated-cases",
        action="store_true",
        help="Executa com geracao de casos sinteticos via LLM (modo completo).",
    )
    parser.add_argument(
        "--without-generated-cases",
        action="store_true",
        help="Executa apenas os casos dev pre-definidos (sem geracao LLM, mais rapido).",
    )
    parser.add_argument(
        "-p",
        "--procedure",
        "--procedures",
        dest="procedures",
        nargs="*",
        help="Filtra rotinas por nome ou prefixo (ex: -p b ou -p b_fn_saldo_cliente).",
    )
    parser.add_argument(
        "-t",
        "--tag",
        type=str,
        default=None,
        help="Rotulo ou tag customizada para identificar esta execucao no historico de tracking.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Exibe a tabela historica de tracking das execucoes salvas e encerra.",
    )
    parser.add_argument(
        "-d",
        "--detail",
        action="store_true",
        help="Exibe o detalhamento de cada caso de teste individual na saida do terminal.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Desativa cores e formatacao rica no terminal.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    console = Console(no_color=args.no_color)

    if args.history:
        _display_history(console, RESULTS)
        return

    settings = Settings()
    if settings.evaluation_database_url is None:
        raise AppError("Set EVALUATION_DATABASE_URL before generating the examples")

    schema = (EXAMPLES / "schema.sql").read_text(encoding="utf-8")
    all_sources = sorted(PROCEDURES.glob("*.sql"))

    sources, generate_cases = _prompt_pre_run_options(
        console,
        all_sources,
        args.procedures,
        args.without_generated_cases,
        args.with_generated_cases,
    )

    if not sources:
        avail = ", ".join(s.stem for s in all_sources)
        console.print("[bold red]Erro:[/] Nenhuma rotina encontrada correspondente aos filtros.")
        console.print(f"[dim]Rotinas disponiveis: {avail}[/]")
        return

    routine_names = [s.stem for s in sources]

    _print_banner(
        console,
        settings,
        routine_names,
        generate_cases=generate_cases,
        tag=args.tag,
    )

    dataset = Dataset.load(settings.evaluation_dataset_file)
    named_commands: list[tuple[str, ModernizeCommand]] = []
    for path in sources:
        source = path.read_text(encoding="utf-8")
        routine = PglastParser().parse(source).name
        behavior = dataset.scenario(routine, include_holdout=False)
        named_commands.append(
            (path.stem, ModernizeCommand(source, schema, behavior, generate_cases))
        )

    start_time = time.monotonic()
    container = build_container(settings)

    try:
        modernize = await container.get(ModernizeRoutine)
        evaluate = await container.get(EvaluateModernization)

        with Progress(
            SpinnerColumn(spinner_name="dots"),
            TextColumn("[bold cyan]{task.fields[routine]:<38}[/]"),
            BarColumn(bar_width=20),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            tasks = {
                name: progress.add_task(
                    "[dim]Aguardando...[/]",
                    routine=name,
                    total=2,
                )
                for name, _ in named_commands
            }

            runs: list[Modernization] = []
            evaluations: list[Evaluation] = []

            for name, cmd in named_commands:
                task_id = tasks[name]
                progress.update(task_id, description="[yellow]Modernizando (LangGraph)...[/]")
                run = await modernize.execute(cmd)
                runs.append(run)

                desc_eval = f"[blue]Modernizado ({run.status.value}), avaliando no PostgreSQL...[/]"
                progress.update(task_id, description=desc_eval, completed=1)
                ev = await evaluate.execute(EvaluateCommand(run.id))
                evaluations.append(ev)

                if ev.equivalent:
                    status_lbl = "[OK]"
                    color = "green"
                elif ev.score > 0:
                    status_lbl = "[PARCIAL]"
                    color = "yellow"
                else:
                    status_lbl = "[FALHA]"
                    color = "red"
                res_desc = (
                    f"[{color}]{status_lbl} {ev.cases_passed}/{ev.cases_total} "
                    f"casos ({ev.score:.0%})[/]"
                )
                progress.update(task_id, description=res_desc, completed=2)
    finally:
        await container.close()

    total_duration = time.monotonic() - start_time
    named = [(name, run) for (name, _), run in zip(named_commands, runs, strict=True)]

    # 1. Persist the run: tracking history plus its snapshot (the only artifacts on disk)
    record = _record_history(
        settings,
        named,
        evaluations,
        [cmd for _, cmd in named_commands],
        generate_cases=generate_cases,
        tag=args.tag,
        duration_seconds=total_duration,
        results_dir=RESULTS,
    )

    # 2. Print screen report
    _print_screen_report(
        console,
        settings,
        named,
        evaluations,
        snapshot_dir=_snapshot_dir(RESULTS, record["run_id"], args.tag),
        generate_cases=generate_cases,
        tag=args.tag,
        total_duration=total_duration,
        show_detail=args.detail,
    )


if __name__ == "__main__":
    asyncio.run(main())
