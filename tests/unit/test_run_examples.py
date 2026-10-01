import json

from app.core.config.settings import Settings
from app.features.modernization.domain import Modernization
from app.features.modernization.evaluation.domain import Evaluation
from app.features.modernization.validation.checks.behavior.domain import CaseResult
from scripts.run_examples import _summary, _write_run


def test_example_artifacts_keep_the_measured_failures(tmp_path):
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
    summary = _summary(Settings(_env_file=None), [("b", run)], [evaluation])
    assert "0/1 rotinas (0%)" in summary
    assert "[0/1 (0%)](b/evaluation.json)" in summary
    assert "edge: result differs" in summary
