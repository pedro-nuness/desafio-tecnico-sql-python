"""Graph nodes: thin adapters that read the state, call one collaborator and return an update.

No business rule lives here: steps delegate to their strategy or step class, recording
delegates to the ExecutionLog.
"""

from typing import ClassVar, Protocol

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.case_generation.generate_cases import GenerateCases
from app.features.modernization.code_generation.domain import RepairFeedback
from app.features.modernization.code_generation.generate_code import GenerateCode
from app.features.modernization.domain import PipelineStep
from app.features.modernization.graph.state import ModernizationState, StateUpdate, to_report
from app.features.modernization.parsing.parser import SQLParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.validation.checks.behavior.domain import Scenario
from app.features.modernization.validation.validate_code import Routine, ValidateCode
from app.shared.errors import AppError
from app.shared.integrations.errors import IntegrationError


class StepNode(Protocol):
    """One pipeline step. The builder registers it under `step` and records its failures."""

    step: ClassVar[PipelineStep]

    async def __call__(self, state: ModernizationState) -> StateUpdate: ...


def require[T](value: T | None, name: str) -> T:
    """A missing upstream result is an orchestration bug, not a user error."""
    if value is None:
        raise AppError(f"{name} missing from the pipeline state", missing=name)
    return value


# --------------------------------------------------------------------------- steps


class ParsingNode:
    step = PipelineStep.PARSING

    def __init__(self, parser: SQLParser) -> None:
        self._parser = parser

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = self._parser.parse(state["source_code"])
        return StateUpdate(parsed_procedure=procedure, completed_steps=[self.step])


class SemanticAnalysisNode:
    step = PipelineStep.SEMANTIC_ANALYSIS

    def __init__(self, analyzer: SemanticAnalyzer) -> None:
        self._analyzer = analyzer

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = require(state.get("parsed_procedure"), "parsed_procedure")
        warnings = (
            []
            if state.get("schema_context")
            else ["No schema provided: column types and constraints are inferred by the LLM."]
        )
        return StateUpdate(
            semantic_analysis=self._analyzer.analyze(procedure),
            warnings=warnings,
            completed_steps=[self.step],
        )


class CodeGenerationNode:
    """On a retry (routed back from validation) the previous code and the validation issues
    go into the prompt as RepairFeedback."""

    step = PipelineStep.CODE_GENERATION

    def __init__(self, generate_code: GenerateCode) -> None:
        self._generate_code = generate_code

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        attempt = state.get("generation_attempts", 0) + 1
        feedback = _feedback(state, attempt)
        code, result = await self._generate_code.execute(
            procedure=require(state.get("parsed_procedure"), "parsed_procedure"),
            analysis=require(state.get("semantic_analysis"), "semantic_analysis"),
            source_code=state["source_code"],
            schema_context=state.get("schema_context"),
            feedback=feedback,
        )
        warnings = (
            [
                f"generation attempt {attempt}: regenerated after {len(feedback.issues)} "
                "validation issue(s) in the previous attempt"
            ]
            if feedback is not None
            else []
        )
        return StateUpdate(
            code_generation=result,
            generated_code=code,
            generation_attempts=attempt,
            completed_steps=[self.step],
            warnings=warnings,
        )


def _feedback(state: ModernizationState, attempt: int) -> RepairFeedback | None:
    previous_code = state.get("generated_code")
    validation = state.get("validation_result")
    if attempt == 1 or previous_code is None or validation is None:
        return None
    return RepairFeedback(attempt=attempt, previous_code=previous_code, issues=validation.issues())


class CaseGenerationNode:
    """Runs next to CodeGenerationNode (fan-out after the analysis). Never fails the run: the
    cases only add verification, so a failure here becomes a warning and the caller's
    scenario is kept as is."""

    step = PipelineStep.CASE_GENERATION

    def __init__(self, generate_cases: GenerateCases) -> None:
        self._generate_cases = generate_cases

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        schema = state.get("schema_context")
        if not schema:
            return StateUpdate(
                warnings=["Case generation skipped: it needs `schema` to build the test data."],
                completed_steps=[self.step],
            )
        # Needed: an LLM failure or an off-contract answer must not fail a run whose code may
        # be fine; the report says the step produced nothing.
        try:
            behavior, result = await self._generate_cases.execute(
                procedure=require(state.get("parsed_procedure"), "parsed_procedure"),
                source_code=state["source_code"],
                schema_context=schema,
                behavior=_behavior(state),
            )
        except IntegrationError as exc:
            return StateUpdate(
                warnings=[f"Case generation failed, only the caller's cases run: {exc.message}"],
                completed_steps=[self.step],
            )
        return StateUpdate(behavior=behavior, case_generation=result, completed_steps=[self.step])


def _behavior(state: ModernizationState) -> Scenario | None:
    behavior = state.get("behavior")
    # The LangGraph API and Studio send the input as plain JSON.
    return None if behavior is None else Scenario.model_validate(behavior)


class ValidationNode:
    step = PipelineStep.VALIDATION

    def __init__(self, validate_code: ValidateCode) -> None:
        self._validate_code = validate_code

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        code = require(state.get("generated_code"), "generated_code")
        routine = Routine(
            source_code=state["source_code"],
            procedure=require(state.get("parsed_procedure"), "parsed_procedure"),
            behavior=_behavior(state),
        )
        result = await self._validate_code.execute(code, routine)
        return StateUpdate(validation_result=result, completed_steps=[self.step])


# --------------------------------------------------------------------------- run recording


class RecordStartNode:
    def __init__(self, execution_log: ExecutionLog) -> None:
        self._execution_log = execution_log

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        started = await self._execution_log.start(state["source_code"], state.get("schema_context"))
        return StateUpdate(
            execution_id=started.id,
            started_at=started.created_at,
            generation_attempts=0,
            status=started.status,
        )


class RecordResultNode:
    def __init__(self, execution_log: ExecutionLog) -> None:
        self._execution_log = execution_log

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        finished = await self._execution_log.complete(
            state["execution_id"], to_report(state), state.get("generated_code")
        )
        return StateUpdate(modernization=finished, status=finished.status)
