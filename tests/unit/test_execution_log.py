from uuid import uuid4

import pytest

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.modernization import ModernizationReport, PipelineError
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.shared.errors import NotFoundError
from app.shared.integrations.errors import IntegrationError
from tests.fakes import InMemoryDatabase, InMemoryModernizationRepository


@pytest.fixture
def log(store: InMemoryDatabase) -> ExecutionLog:
    return ExecutionLog(InMemoryModernizationRepository(store))


async def test_start_commits_a_running_row(log: ExecutionLog, store: InMemoryDatabase) -> None:
    started = await log.start("src", None)

    assert store.rows[started.id].status is ModernizationStatus.RUNNING


async def test_fail_is_always_a_failure_and_keeps_the_error_payload(
    log: ExecutionLog, store: InMemoryDatabase
) -> None:
    started = await log.start("src", None)
    error = PipelineError.from_exception(
        PipelineStep.GENERATION, IntegrationError("llm down", upstream_status=503)
    )

    failed = await log.fail(started.id, ModernizationReport(), None, error)

    assert failed.status is ModernizationStatus.FAILURE
    assert store.rows[started.id] == failed
    [recorded] = failed.report.errors
    assert recorded.payload == {"upstream_status": 503}


@pytest.mark.parametrize("finish", ["complete", "fail"])
async def test_finishing_an_unknown_run_raises(log: ExecutionLog, finish: str) -> None:
    execution_id = uuid4()
    with pytest.raises(NotFoundError):
        if finish == "complete":
            await log.complete(execution_id, ModernizationReport(), None)
        else:
            error = PipelineError(step=None, error_type="X", message="boom")
            await log.fail(execution_id, ModernizationReport(), None, error)
