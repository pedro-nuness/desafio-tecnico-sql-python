"""Deterministic semantic analysis over the parser-agnostic IR.

Pure logic: no I/O, no parser library. It is intentionally not behind a port — there is a
single, deterministic implementation and nothing external to swap. Catalogs live in
catalog.py; risks and recommendations in risks.py.
"""

from collections import defaultdict

from app.features.modernization.analysis.catalog import (
    AGGREGATE_FUNCTIONS,
    DML_COMMANDS,
    PROCEDURAL_FEATURES,
    STATEMENT_FEATURES,
    is_builtin_function,
)
from app.features.modernization.analysis.risks import (
    detect_risks,
    fragments_with_lines,
    is_dynamic_sql,
    recommend,
)
from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.parsing import (
    LOOP_KINDS,
    DeclarationKind,
    ParameterMode,
    ParsedProcedure,
    SqlFragment,
    Statement,
)
from app.features.modernization.domain.semantic_analysis import (
    Dependency,
    DependencyKind,
    DetectedFeature,
    Feature,
    ParameterSummary,
    SemanticAnalysis,
)


class _FeatureCollector:
    def __init__(self) -> None:
        self._lines: dict[Feature, list[int]] = defaultdict(list)
        self._counts: dict[Feature, int] = defaultdict(int)

    def add(self, feature: Feature, line: int | None = None, occurrences: int = 1) -> None:
        self._counts[feature] += occurrences
        if line is not None and line not in self._lines[feature]:
            self._lines[feature].append(line)

    def __contains__(self, feature: object) -> bool:
        return feature in self._counts

    def result(self) -> tuple[DetectedFeature, ...]:
        return tuple(
            DetectedFeature(feature=feature, occurrences=count, lines=tuple(self._lines[feature]))
            for feature, count in sorted(self._counts.items())
        )


class SemanticAnalyzer:
    def analyze(self, procedure: ParsedProcedure) -> SemanticAnalysis:
        features = _FeatureCollector()
        parameters = _summarize_parameters(procedure)
        variables = _collect_declarations(procedure, features)
        if parameters.inputs or parameters.in_out:
            features.add(
                Feature.IN_PARAMETERS, occurrences=len(parameters.inputs + parameters.in_out)
            )
        if parameters.outputs or parameters.in_out:
            features.add(
                Feature.OUT_PARAMETERS, occurrences=len(parameters.outputs + parameters.in_out)
            )
        if any("jsonb" in parameter.data_type.lower() for parameter in procedure.parameters):
            features.add(Feature.JSONB)

        for statement, _ in procedure.iter_statements():
            _collect_statement_features(statement, features)
        for fragment, line in fragments_with_lines(procedure):
            _collect_sql_features(fragment, line, features)

        risks = detect_risks(procedure, features)
        strategy = _recommend_strategy(procedure, features)
        return SemanticAnalysis(
            features=features.result(),
            risks=risks,
            dependencies=_collect_dependencies(procedure),
            parameters=parameters,
            variables=variables,
            recommended_strategy=strategy,
            recommendations=recommend(features, risks),
        )


# --------------------------------------------------------------------------- features


def _summarize_parameters(procedure: ParsedProcedure) -> ParameterSummary:
    def names(*modes: ParameterMode) -> tuple[str, ...]:
        return tuple(p.name for p in procedure.parameters if p.mode in modes)

    return ParameterSummary(
        inputs=names(ParameterMode.IN, ParameterMode.VARIADIC),
        outputs=names(ParameterMode.OUT, ParameterMode.TABLE),
        in_out=names(ParameterMode.INOUT),
    )


def _collect_declarations(
    procedure: ParsedProcedure, features: _FeatureCollector
) -> tuple[str, ...]:
    variables: list[str] = []
    for declaration in procedure.declarations:
        if declaration.kind is DeclarationKind.CURSOR:
            features.add(Feature.CURSOR, declaration.line)
            continue
        variables.append(declaration.name)
        features.add(Feature.VARIABLES, declaration.line)
        if declaration.data_type and "jsonb" in declaration.data_type.lower():
            features.add(Feature.JSONB, declaration.line)
    return tuple(variables)


def _collect_statement_features(statement: Statement, features: _FeatureCollector) -> None:
    if statement.kind in LOOP_KINDS:
        features.add(Feature.LOOP, statement.line)
    if is_dynamic_sql(statement):
        features.add(Feature.DYNAMIC_SQL, statement.line)
    if feature := STATEMENT_FEATURES.get(statement.kind):
        features.add(feature, statement.line)
    if statement.exception_handlers:
        features.add(Feature.EXCEPTION_HANDLING, statement.line)


def _collect_sql_features(
    fragment: SqlFragment, line: int | None, features: _FeatureCollector
) -> None:
    if fragment.command in DML_COMMANDS:
        features.add(Feature.DML, line)
    if fragment.locking_clauses:
        features.add(Feature.ROW_LOCKING, line)
    if fragment.uses_jsonb:
        features.add(Feature.JSONB, line)
    if fragment.has_cte:
        features.add(Feature.CTE, line)
    if fragment.has_recursive_cte:
        features.add(Feature.RECURSIVE_CTE, line)
    if fragment.has_join:
        features.add(Feature.JOIN, line)
    if any(f.rpartition(".")[2].lower() in AGGREGATE_FUNCTIONS for f in fragment.functions):
        features.add(Feature.AGGREGATION, line)
    if any(not is_builtin_function(f) for f in fragment.functions):
        features.add(Feature.FUNCTION_CALLS, line)


# --------------------------------------------------------------------------- strategy, dependencies


def _recommend_strategy(
    procedure: ParsedProcedure, features: _FeatureCollector
) -> GenerationStrategy:
    has_sql = next(procedure.sql_fragments(), None) is not None
    is_procedural = any(feature in features for feature in PROCEDURAL_FEATURES)
    if not has_sql:
        return GenerationStrategy.PYTHON_REIMPLEMENTATION
    if not is_procedural:
        return GenerationStrategy.DATABASE_DELEGATED
    return GenerationStrategy.HYBRID


def _collect_dependencies(procedure: ParsedProcedure) -> tuple[Dependency, ...]:
    tables = [Dependency(name=t, kind=DependencyKind.TABLE) for t in procedure.referenced_tables]
    functions = [
        Dependency(name=f, kind=DependencyKind.FUNCTION)
        for f in procedure.called_functions
        if not is_builtin_function(f)
    ]
    return tuple(tables + functions)
