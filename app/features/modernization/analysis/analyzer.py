"""Deterministic semantic analysis over the parser-agnostic IR.

Pure logic: no I/O, no parser library. It is intentionally not behind a port — there is a
single, deterministic implementation and nothing external to swap. Catalogs live in
catalog.py; risks and recommendations in risks.py.
"""

from collections import defaultdict

from app.features.modernization.analysis.catalog import (
    AGGREGATE_FUNCTIONS,
    DML_COMMANDS,
    PROCEDURAL_CONSTRUCTS,
    STATEMENT_CONSTRUCTS,
    is_builtin_function,
)
from app.features.modernization.analysis.domain import (
    Dependency,
    DependencyKind,
    DetectedSqlConstruct,
    GenerationStrategy,
    ParameterSummary,
    SemanticAnalysis,
    SqlConstruct,
)
from app.features.modernization.analysis.risks import (
    detect_risks,
    fragments_with_lines,
    is_dynamic_sql,
    recommend,
)
from app.features.modernization.parsing.domain import (
    LOOP_KINDS,
    DeclarationKind,
    ParameterMode,
    ParsedProcedure,
    SqlFragment,
    Statement,
)


class _SqlConstructCollector:
    def __init__(self) -> None:
        self._lines: dict[SqlConstruct, list[int]] = defaultdict(list)
        self._counts: dict[SqlConstruct, int] = defaultdict(int)

    def add(self, construct: SqlConstruct, line: int | None = None, occurrences: int = 1) -> None:
        self._counts[construct] += occurrences
        if line is not None and line not in self._lines[construct]:
            self._lines[construct].append(line)

    def __contains__(self, construct: object) -> bool:
        return construct in self._counts

    def result(self) -> tuple[DetectedSqlConstruct, ...]:
        return tuple(
            DetectedSqlConstruct(
                construct=construct, occurrences=count, lines=tuple(self._lines[construct])
            )
            for construct, count in sorted(self._counts.items())
        )


class SemanticAnalyzer:
    def analyze(self, procedure: ParsedProcedure) -> SemanticAnalysis:
        constructs = _SqlConstructCollector()
        parameters = _summarize_parameters(procedure)
        variables = _collect_declarations(procedure, constructs)
        if parameters.inputs or parameters.in_out:
            constructs.add(
                SqlConstruct.IN_PARAMETERS, occurrences=len(parameters.inputs + parameters.in_out)
            )
        if parameters.outputs or parameters.in_out:
            constructs.add(
                SqlConstruct.OUT_PARAMETERS, occurrences=len(parameters.outputs + parameters.in_out)
            )
        if any("jsonb" in parameter.data_type.lower() for parameter in procedure.parameters):
            constructs.add(SqlConstruct.JSONB)

        for statement, _ in procedure.iter_statements():
            _collect_statement_constructs(statement, constructs)
        for fragment, line in fragments_with_lines(procedure):
            _collect_sql_constructs(fragment, line, constructs)

        risks = detect_risks(procedure, constructs)
        strategy = _recommend_strategy(procedure, constructs)
        return SemanticAnalysis(
            constructs=constructs.result(),
            risks=risks,
            dependencies=_collect_dependencies(procedure),
            parameters=parameters,
            variables=variables,
            recommended_strategy=strategy,
            recommendations=recommend(constructs, risks),
        )


# --------------------------------------------------------------------------- constructs


def _summarize_parameters(procedure: ParsedProcedure) -> ParameterSummary:
    def names(*modes: ParameterMode) -> tuple[str, ...]:
        return tuple(p.name for p in procedure.parameters if p.mode in modes)

    return ParameterSummary(
        inputs=names(ParameterMode.IN, ParameterMode.VARIADIC),
        outputs=names(ParameterMode.OUT, ParameterMode.TABLE),
        in_out=names(ParameterMode.INOUT),
    )


def _collect_declarations(
    procedure: ParsedProcedure, constructs: _SqlConstructCollector
) -> tuple[str, ...]:
    variables: list[str] = []
    for declaration in procedure.declarations:
        if declaration.kind is DeclarationKind.CURSOR:
            constructs.add(SqlConstruct.CURSOR, declaration.line)
            continue
        variables.append(declaration.name)
        constructs.add(SqlConstruct.VARIABLES, declaration.line)
        if declaration.data_type and "jsonb" in declaration.data_type.lower():
            constructs.add(SqlConstruct.JSONB, declaration.line)
    return tuple(variables)


def _collect_statement_constructs(statement: Statement, constructs: _SqlConstructCollector) -> None:
    if statement.kind in LOOP_KINDS:
        constructs.add(SqlConstruct.LOOP, statement.line)
    if is_dynamic_sql(statement):
        constructs.add(SqlConstruct.DYNAMIC_SQL, statement.line)
    if construct := STATEMENT_CONSTRUCTS.get(statement.kind):
        constructs.add(construct, statement.line)
    if statement.exception_handlers:
        constructs.add(SqlConstruct.EXCEPTION_HANDLING, statement.line)


def _collect_sql_constructs(
    fragment: SqlFragment, line: int | None, constructs: _SqlConstructCollector
) -> None:
    if fragment.command in DML_COMMANDS:
        constructs.add(SqlConstruct.DML, line)
    if fragment.locking_clauses:
        constructs.add(SqlConstruct.ROW_LOCKING, line)
    if fragment.uses_jsonb:
        constructs.add(SqlConstruct.JSONB, line)
    if fragment.has_cte:
        constructs.add(SqlConstruct.CTE, line)
    if fragment.has_recursive_cte:
        constructs.add(SqlConstruct.RECURSIVE_CTE, line)
    if fragment.has_join:
        constructs.add(SqlConstruct.JOIN, line)
    if any(f.rpartition(".")[2].lower() in AGGREGATE_FUNCTIONS for f in fragment.functions):
        constructs.add(SqlConstruct.AGGREGATION, line)
    if any(not is_builtin_function(f) for f in fragment.functions):
        constructs.add(SqlConstruct.FUNCTION_CALLS, line)


# --------------------------------------------------------------------------- strategy, dependencies


def _recommend_strategy(
    procedure: ParsedProcedure, constructs: _SqlConstructCollector
) -> GenerationStrategy:
    has_sql = next(procedure.sql_fragments(), None) is not None
    is_procedural = any(construct in constructs for construct in PROCEDURAL_CONSTRUCTS)
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
