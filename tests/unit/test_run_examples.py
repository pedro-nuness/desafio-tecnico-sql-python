import json
from datetime import date
from pathlib import Path

from rich.console import Console

from app.core.config.settings import Settings
from app.features.modernization.domain import Modernization
from app.features.modernization.evaluation.domain import Evaluation
from app.features.modernization.use_cases import ModernizeCommand
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseResult,
    Scenario,
)
from scripts.run_examples import (
    _display_history,
    _filter_procedures,
    _print_screen_report,
    _record_history,
    _summary,
    _write_run,
)


def test_example_artifacts_keep_the_measured_failures(tmp_path: Path) -> None:
    run = Modernization.start("SELECT 1")
    evaluation = Evaluation.of(
        run,
        (
            CaseResult(
                name="edge", passed=False, detail="result differs", original="1", generated="2"
            ),
        ),
    )
    _write_run(tmp_path, run, evaluation)
    recorded = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert recorded["modernization_id"] == str(run.id)
    assert recorded["cases"][0]["passed"] is False
    summary = _summary(Settings(_env_file=None), [("b", run)], [evaluation], generate_cases=False)
    assert "0/1 rotinas (0%)" in summary
    assert "[0/1 (0%)](b/evaluation.json)" in summary
    assert "edge: result differs" in summary


def test_record_history_persists_jsonl_and_updates_markdown(tmp_path: Path) -> None:
    run = Modernization.start("SELECT 1")
    evaluation = Evaluation.of(
        run,
        (
            CaseResult(name="c1", passed=True, detail="ok", original="1", generated="1"),
            CaseResult(
                name="c2", passed=False, detail="diff", original="1", generated="2", holdout=True
            ),
        ),
    )
    settings = Settings(_env_file=None)
    ddl = "CREATE TABLE t (\n  d date\n)"
    behavior = Scenario(
        setup_sql=ddl,
        cases=(Case(name="c1", sql="SELECT fn(1)", args=(date(2026, 9, 15),)),),
    )
    command = ModernizeCommand("SELECT 1", ddl, behavior, True)
    record = _record_history(
        settings,
        [("b_fn_saldo_cliente", run)],
        [evaluation],
        [command],
        generate_cases=True,
        tag="test-tag",
        duration_seconds=12.5,
        results_dir=tmp_path,
    )

    assert record["tag"] == "test-tag"
    assert record["routines_count"] == 1
    assert record["cases_passed"] == 1
    assert record["cases_total"] == 2
    assert record["holdout_passed"] == 0
    assert record["holdout_total"] == 1

    # Check jsonl
    history_file = tmp_path / "history.jsonl"
    assert history_file.exists()
    lines = history_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    saved_record = json.loads(lines[0])
    assert saved_record["run_id"] == record["run_id"]

    # Check HISTORY.md
    history_md = tmp_path / "HISTORY.md"
    assert history_md.exists()
    md_content = history_md.read_text(encoding="utf-8")
    assert "test-tag" in md_content
    assert "1/2 (50.0%)" in md_content

    # Check snapshot
    snapshot_dir = tmp_path / "history" / f"run_{record['run_id']}_test-tag"
    assert snapshot_dir.exists()
    assert (snapshot_dir / "run.json").exists()
    assert (snapshot_dir / "SUMMARY.md").exists()

    # Each routine keeps what it received and what it returned in this run
    procedure_dir = snapshot_dir / "b_fn_saldo_cliente"
    assert (procedure_dir / "procedure.sql").read_text(encoding="utf-8") == "SELECT 1"
    payload = json.loads((procedure_dir / "payload.json").read_text(encoding="utf-8"))
    assert payload["source_code"] == ["SELECT 1"]
    assert payload["schema_context"] == ["CREATE TABLE t (", "  d date", ")"]
    assert payload["behavior"]["setup_sql"] == ["CREATE TABLE t (", "  d date", ")"]
    assert payload["behavior"]["cases"][0]["args"] == ["2026-09-15"]
    assert payload["generate_cases"] is True
    assert (procedure_dir / "report.json").exists()
    assert (procedure_dir / "evaluation.json").exists()


def test_filter_procedures_by_prefix_and_name() -> None:
    sources = [
        Path("examples/procedures/b_fn_saldo_cliente.sql"),
        Path("examples/procedures/c_sp_atualizar_status_contas_inativas.sql"),
        Path("examples/procedures/d_sp_transferir_entre_contas.sql"),
    ]

    # No filter returns all
    assert _filter_procedures(sources, None) == sources
    assert _filter_procedures(sources, []) == sources

    # Filter by single letter prefix
    b_only = _filter_procedures(sources, ["b"])
    assert len(b_only) == 1
    assert b_only[0].stem == "b_fn_saldo_cliente"

    # Filter by comma-separated string
    bc = _filter_procedures(sources, ["b,c"])
    assert len(bc) == 2
    assert [p.stem for p in bc] == [
        "b_fn_saldo_cliente",
        "c_sp_atualizar_status_contas_inativas",
    ]

    # Filter by multiple args
    cd = _filter_procedures(sources, ["c", "d"])
    assert len(cd) == 2


def test_display_history_and_screen_report(tmp_path: Path) -> None:
    console = Console(record=True, width=120)
    # When empty
    _display_history(console, results_dir=tmp_path)
    output = console.export_text()
    assert "Nenhuma execucao encontrada" in output

    # Populate and display
    run = Modernization.start("SELECT 1")
    evaluation = Evaluation.of(
        run,
        (
            CaseResult(name="dev1", passed=True, detail="ok", original="1", generated="1"),
            CaseResult(
                name="h1", passed=False, detail="diff", original="1", generated="2", holdout=True
            ),
        ),
    )
    _record_history(
        Settings(_env_file=None),
        [("b_fn_saldo_cliente", run)],
        [evaluation],
        [ModernizeCommand("SELECT 1")],
        generate_cases=True,
        tag="sample",
        duration_seconds=5.0,
        results_dir=tmp_path,
    )

    console_hist = Console(record=True, width=120)
    _display_history(console_hist, results_dir=tmp_path)
    hist_output = console_hist.export_text()
    assert "sample" in hist_output

    # Test screen report with detail=True
    console_report = Console(record=True, width=120)
    _print_screen_report(
        console_report,
        Settings(_env_file=None),
        [("b_fn_saldo_cliente", run)],
        [evaluation],
        snapshot_dir=tmp_path / "history" / "run_x_sample",
        generate_cases=True,
        tag="sample",
        total_duration=5.0,
        show_detail=True,
    )
    rep_output = console_report.export_text()
    assert "Scorecard Consolidado" in rep_output
    assert "b_fn_saldo_cliente" in rep_output
    assert "Divergencias Comportamentais" in rep_output
